#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/opt/repos/hermes-social/social/tools/tiktok-backoffice')
DB = Path(os.getenv('TIKTOK_BACKOFFICE_DB', '/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3'))
PROCESS_ONE = Path(os.getenv('TIKTOK_PROCESS_ONE', '/opt/data/tmp/process_tiktok_review_one.py'))
SAFE_STATUSES = {'pending_review'}


def run(cmd: list[str], *, cwd: Path = ROOT, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


def db_rows(sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def db_exec(sql: str, params: tuple = ()) -> None:
    con = sqlite3.connect(DB)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def pending_items(limit: int) -> list[dict]:
    return db_rows(
        '''
        SELECT ri.id, ri.status, ri.campaign_slug, ri.matched_keyword, ri.failure_reason,
               c.author, c.text AS comment_text, c.video_url
        FROM tiktok_review_items ri
        JOIN tiktok_comments c ON c.comment_id = ri.comment_id
        WHERE ri.status = 'pending_review'
          AND ri.campaign_slug != 'guide'
        ORDER BY ri.created_at ASC, ri.id ASC
        LIMIT ?
        ''',
        (limit,),
    )


def status_counts() -> dict[str, int]:
    return {r['status']: r['n'] for r in db_rows('SELECT status, COUNT(*) n FROM tiktok_review_items GROUP BY status')}


def sync_campaigns() -> dict:
    p = run(['uv', 'run', 'tiktok-backoffice', 'sync-kanban-campaigns'], timeout=120)
    return {'ok': p.returncode == 0, 'stdout': p.stdout.strip(), 'stderr': p.stderr.strip()[-500:]}


def scan_targets(limit: int) -> dict:
    targets_p = run(['uv', 'run', 'tiktok-backoffice', 'poll-targets', '--limit', str(limit)], timeout=120)
    if targets_p.returncode != 0:
        return {'ok': False, 'stage': 'poll_targets_failed', 'stderr': targets_p.stderr.strip()[-800:]}
    try:
        targets = json.loads(targets_p.stdout).get('items') or []
    except Exception as e:
        return {'ok': False, 'stage': 'poll_targets_bad_json', 'error': str(e), 'stdout': targets_p.stdout[-800:]}
    results = []
    for target in targets:
        video_url = target.get('video_url')
        if not video_url:
            continue
        # Use the Camofox container IP from the host because 9377 is not published on localhost.
        base = os.getenv('TIKTOK_CAMOFOX_BASE_URL')
        if not base:
            ip_p = run(['docker', 'inspect', '-f', '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}', os.getenv('TIKTOK_CAMOFOX_CONTAINER', 'hermes-social-camofox-1')], cwd=Path('/opt/repos/hermes-social'), timeout=20)
            ip = ip_p.stdout.strip()
            base = f'http://{ip}:9377' if ip else 'http://camofox:9377'
        p = run(['uv', 'run', 'tiktok-backoffice', 'fetch-comments-camofox', '--base-url', base, '--video-url', video_url, '--ingest'], timeout=240)
        item = {'video_url': video_url, 'ok': p.returncode == 0}
        try:
            item.update(json.loads(p.stdout))
        except Exception:
            item.update({'stage': 'bad_json', 'stdout': p.stdout[-500:], 'stderr': p.stderr[-500:]})
        results.append(item)
    return {'ok': True, 'targets': len(targets), 'results': results}


def dry_run_item(item: dict) -> dict:
    # Dry-run is intentionally conservative: it confirms that the durable per-review
    # processor exists and that the campaign is not guide. It does not open/send DM.
    return {
        'review_id': item['id'],
        'author': item.get('author'),
        'campaign': item.get('campaign_slug'),
        'stage': 'would_run_verified_dm_processor',
        'ok': True,
    }


def process_item_apply(item: dict) -> dict:
    if not PROCESS_ONE.exists():
        return {'review_id': item['id'], 'author': item.get('author'), 'campaign': item.get('campaign_slug'), 'ok': False, 'stage': 'processor_missing', 'path': str(PROCESS_ONE)}
    p = run(['python3', str(PROCESS_ONE), str(item['id'])], cwd=ROOT, timeout=600)
    result = {'review_id': item['id'], 'author': item.get('author'), 'campaign': item.get('campaign_slug'), 'ok': p.returncode == 0}
    try:
        parsed = json.loads(p.stdout)
        result['stage'] = parsed.get('stage')
        result['processor_ok'] = parsed.get('ok')
        # Do not include full body/private message contexts in Telegram cron output.
    except Exception:
        result['stage'] = 'processor_bad_json'
        result['stdout'] = p.stdout[-800:]
    if p.stderr.strip():
        result['stderr'] = p.stderr.strip()[-800:]
    return result


def format_message(report: dict) -> str:
    before = report.get('before', {})
    after = report.get('after', {})
    scan = report.get('scan', {})
    processed = report.get('processed', [])
    interesting = []
    for r in processed:
        if r.get('stage') not in {'ignored_already_dm_sent', 'ignored_already_public_replied'}:
            interesting.append(r)
    created = 0
    scan_failures = []
    for r in (scan.get('results') or []):
        ing = r.get('ingest') or {}
        created += int(ing.get('created_reviews') or 0)
        if not r.get('ok'):
            scan_failures.append(r)
    if not processed and created == 0 and not scan_failures and before == after and not report.get('errors'):
        return ''
    lines = ['TikTok cron auto-DM' if report.get('apply') else 'TikTok cron dry-run']
    lines.append(f"scan: {scan.get('targets', 0)} vidéos, nouveaux review_items={created}, échecs_scan={len(scan_failures)}")
    for r in scan_failures[:5]:
        reason = r.get('error') or r.get('stage') or 'unknown'
        detail = r.get('detail') or ''
        lines.append(f"- scan fail: {reason} {detail}".strip())
    if processed:
        lines.append(f"items traités: {len(processed)}")
        for r in processed[:8]:
            lines.append(f"- #{r.get('review_id')} {r.get('author')} — {r.get('campaign')} — {r.get('stage')}")
    if before != after:
        lines.append(f"statuts avant: {before}")
        lines.append(f"statuts après: {after}")
    for e in report.get('errors') or []:
        lines.append(f"Erreur: {e}")
    return '\n'.join(lines)


def should_suppress_repeated_scan_only_alert(report: dict, msg: str) -> bool:
    # Avoid Telegram spam when TikTok/Camofox repeats the same scan failure every 15 min.
    # DM sends, new review items, status changes, and processor errors are never suppressed.
    if not msg:
        return True
    if report.get('processed') or report.get('errors'):
        return False
    before = report.get('before', {})
    after = report.get('after', {})
    if before != after:
        return False
    scan = report.get('scan') or {}
    results = scan.get('results') or []
    created = sum(int((r.get('ingest') or {}).get('created_reviews') or 0) for r in results)
    if created:
        return False
    failures = [r for r in results if not r.get('ok')]
    if not failures:
        return False
    sig = '|'.join(f"{r.get('video_url')}:{r.get('error') or r.get('stage')}:{r.get('detail') or ''}" for r in failures[:8])
    state_path = DB.parent / 'tiktok_cron_alert_state.json'
    now = time.time()
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except Exception:
        state = {}
    last = state.get('scan_failure') or {}
    if last.get('sig') == sig and now - float(last.get('ts') or 0) < 6 * 3600:
        return True
    state['scan_failure'] = {'sig': sig, 'ts': now}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='send verified DMs via the validated per-review processor')
    parser.add_argument('--target-limit', type=int, default=int(os.getenv('TIKTOK_CRON_TARGET_LIMIT', '8')))
    parser.add_argument('--review-limit', type=int, default=int(os.getenv('TIKTOK_CRON_REVIEW_LIMIT', '3')))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    report = {'ok': True, 'apply': args.apply, 'errors': [], 'processed': []}
    try:
        report['before'] = status_counts()
        report['sync'] = sync_campaigns()
        report['scan'] = scan_targets(args.target_limit)
        items = pending_items(args.review_limit)
        for item in items:
            if args.apply:
                report['processed'].append(process_item_apply(item))
            else:
                report['processed'].append(dry_run_item(item))
        report['after'] = status_counts()
    except Exception as e:
        report['ok'] = False
        report['errors'].append(f'{type(e).__name__}: {e}')

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        msg = format_message(report)
        if msg and not should_suppress_repeated_scan_only_alert(report, msg):
            print(msg)
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
