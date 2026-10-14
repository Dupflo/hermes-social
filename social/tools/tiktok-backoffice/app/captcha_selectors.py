"""TikTok captcha DOM: selectors and passive page scripts of the captcha solver (s22, console ADR 053).

This module is the ONLY place that knows the captcha DOM, so a selector can be fixed without touching the
solver (app/captcha_solver.py) or the decisions (app/captcha_geometry.py). The selectors come from the
research table of hermes-console s22 ("Verified APIs", 8 variants), rewritten from the official client
(gbiz123/tiktok-captcha-solver 0.8.9, selectors.py — reference only, that repository has no licence).
None of them could be checked against the real TikTok: a changed DOM ends `unrecognized`, never a random click.

Every script is passive: it only reads (presence, image bytes, rectangles, a style value). No navigation,
no click, no event, no value written.
"""

from __future__ import annotations

import json

WRAPPER_V1 = ".captcha-disable-scroll"
WRAPPER_V2 = ".captcha-verify-container"

# Unique identifier of each variant (submit buttons are shared by shapes and icon: the image src decides).
IDENTIFIERS = {
    "puzzle_v1": ".captcha-disable-scroll img.captcha_verify_img_slide",
    "puzzle_v2": ".captcha-verify-container #captcha-verify-image",
    "rotate_v1": ".captcha-disable-scroll [data-testid=whirl-inner-img]",
    "rotate_v2": ".captcha-verify-container > div > div > div > img.cap-absolute",
    "submit_v1": ".captcha-disable-scroll .verify-captcha-submit-button",
    "submit_v2": ".captcha-verify-container .cap-relative button.cap-w-full",
}

_IMAGE_V1 = "#captcha-verify-image"
_IMAGE_V2 = ".captcha-verify-container div.cap-relative img"
_SUBMIT_V1 = ".verify-captcha-submit-button"
_SUBMIT_V2 = ".captcha-verify-container .cap-relative button.cap-w-full"
_DRAG_ICON = ".secsdk-captcha-drag-icon"

ELEMENTS: dict[str, dict[str, str]] = {
    "puzzle_v1": {"puzzle": "#captcha-verify-image", "piece": "img.captcha_verify_img_slide", "handle": _DRAG_ICON},
    "puzzle_v2": {
        "puzzle": "#captcha-verify-image",
        "piece": ".captcha-verify-container .cap-absolute img",
        "handle": _DRAG_ICON,
        "watch": ".captcha-verify-container div[draggable=true]:has(img[draggable=false])",
    },
    "rotate_v1": {
        "outer": "[data-testid=whirl-outer-img]",
        "inner": "[data-testid=whirl-inner-img]",
        "bar": ".captcha_verify_slide--slidebar",
        "handle": _DRAG_ICON,
    },
    "rotate_v2": {
        "outer": ".captcha-verify-container > div > div > div > img:first-child",
        "inner": ".captcha-verify-container > div > div > div > img.cap-absolute",
        "bar": ".captcha-verify-container > div > div > div.cap-w-full > div.cap-rounded-full",
        "handle": ".captcha-verify-container div[draggable=true]",
    },
    "shapes_v1": {"image": _IMAGE_V1, "submit": _SUBMIT_V1},
    "shapes_v2": {"image": _IMAGE_V2, "submit": _SUBMIT_V2},
    "icon_v1": {"image": _IMAGE_V1, "submit": _SUBMIT_V1, "challenge": ".captcha_verify_bar"},
    "icon_v2": {"image": _IMAGE_V2, "submit": _SUBMIT_V2, "challenge": ".captcha-verify-container > div > div > span"},
}

# The captcha's own button for a new image (rotate v2, between two tries); the page itself is never loaded again.
REFRESH_BUTTON = "#captcha_refresh_button"

IMAGE_MAX_BYTES = 2 * 1024 * 1024
CHALLENGE_MAX_CHARS = 200

_IMAGES = {"puzzle": ("puzzle", "piece"), "rotate": ("outer", "inner"), "shapes": ("image",), "icon": ("image",)}
_RECTS = {"puzzle": ("puzzle", "handle"), "rotate": ("bar", "handle"), "shapes": ("image",), "icon": ("image",)}

# Visible = rendered (client rects) and not hidden. Shared by every script.
_HELPERS = (
    " const vis = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';"
    " const all = (s) => Array.from(document.querySelectorAll(s));"
    " const first = (s) => all(s).find(vis) || null;"
    " const any = (s) => all(s).some(vis);"
)

PRESENT_JS = "(() => {" + _HELPERS + f" return any({json.dumps(WRAPPER_V1 + ', ' + WRAPPER_V2)});" + " })()"

# s25 (console ADR 059): rectangle {x, y, width, height} of the first VISIBLE REFRESH_BUTTON, or null. Read again
# right before the solver clicks it through the console-mouse plugin: never a stale element, never a JS click.
REFRESH_JS = (
    "(() => {"
    + _HELPERS
    + f" const el = first({json.dumps(REFRESH_BUTTON)}); if (!el) return null; const r = el.getBoundingClientRect();"
    " return { x: r.x, y: r.y, width: r.width, height: r.height };"
    " })()"
)

FACTS_JS = (
    "(() => {"
    + _HELPERS
    + " const kind = (s) => { const el = first(s); const src = el ? String(el.currentSrc || el.src || '') : '';"
    ' return src.includes("/icon") ? "icon" : (src.includes("/3d") ? "3d" : "other"); };'
    f" const text = (s) => {{ const el = first(s); return el ? String(el.textContent || '').trim().slice(0, {CHALLENGE_MAX_CHARS}) : ''; }};"
    " const f = {"
    f" present: any({json.dumps(WRAPPER_V1 + ', ' + WRAPPER_V2)}),"
    + "".join(f" {key}: any({json.dumps(selector)})," for key, selector in IDENTIFIERS.items())
    + " };"
    f" f.img_src_kind = f.submit_v1 ? kind({json.dumps(_IMAGE_V1)}) : (f.submit_v2 ? kind({json.dumps(_IMAGE_V2)}) : 'other');"
    f" f.challenge = f.submit_v1 ? text({json.dumps(ELEMENTS['icon_v1']['challenge'])})"
    f" : (f.submit_v2 ? text({json.dumps(ELEMENTS['icon_v2']['challenge'])}) : '');"
    " return f;"
    " })()"
)


def _kind(variant: str) -> str:
    return variant.split("_")[0]


def image_selectors(variant: str) -> tuple[str, ...]:
    """Selectors of the images sent to SadCaptcha, in the order of the API call."""
    elements = ELEMENTS[variant]
    return tuple(elements[name] for name in _IMAGES[_kind(variant)])


def rect_names(variant: str) -> tuple[str, ...]:
    """Names of the rectangles geometry_js returns for the variant."""
    if variant not in ELEMENTS:
        raise KeyError(variant)
    return _RECTS[_kind(variant)]


def image_js(selector: str) -> str:
    """Async IIFE: fetch the image in the page, base64 without prefix ({b64}), or {error[, src]}."""
    return (
        "(async () => {"
        + _HELPERS
        + f" const el = first({json.dumps(selector)}) || document.querySelector({json.dumps(selector)});"
        " if (!el) return { error: 'missing' };"
        " const src = String(el.currentSrc || el.src || '');"
        " if (!src) return { error: 'missing' };"
        " try {"
        " const res = await fetch(src);"
        " if (!res.ok) return { error: 'http', src };"
        " const blob = await res.blob();"
        f" if (blob.size > {IMAGE_MAX_BYTES}) return {{ error: 'too_large', src }};"
        " const data = await new Promise((resolve, reject) => {"
        " const reader = new FileReader();"
        " reader.onloadend = () => resolve(String(reader.result || ''));"
        " reader.onerror = () => reject(new Error('read'));"
        " reader.readAsDataURL(blob);"
        " });"
        " return { b64: data.replace(/^data:[^,]*,/, '') };"
        " } catch (e) { return { error: 'fetch', src }; }"
        " })()"
    )


def geometry_js(variant: str) -> str:
    """Rectangles {x, y, width, height} (viewport CSS px) of the variant's named elements; translate_x for puzzle v2."""
    elements = ELEMENTS[variant]
    names = _RECTS[_kind(variant)]
    rects = "".join(f" {name}: rect({json.dumps(elements[name])})," for name in names)
    translate = ""
    if variant == "puzzle_v2":
        translate = (
            f" const w = first({json.dumps(elements['watch'])}) || document.querySelector({json.dumps(elements['watch'])});"
            " const m = w ? /translateX\\((-?\\d+(?:\\.\\d+)?)/.exec(String(w.getAttribute('style') || '')) : null;"
            " out.translate_x = m ? Math.trunc(Number(m[1])) : null;"
        )
    probe = _ROTATE_V2_PROBE if variant == "rotate_v2" else ""
    return (
        "(() => {"
        + _HELPERS
        + " const rect = (s) => { const el = first(s); if (!el) return null; const r = el.getBoundingClientRect();"
        " return { x: r.x, y: r.y, width: r.width, height: r.height }; };"
        " const out = { rects: {" + rects + " } };"
        + translate
        + probe
        + " return out;"
        " })()"
    )


# s27 (console ADR 061): numbers and codes read with the rotate v2 rectangles, before the drag and button held.
# Rotation of an image: the inline `rotate(<n>deg)` of its style attribute (sign and values > 180° kept) ⇒ inline;
# else atan2(b, a) of its computed matrix ⇒ matrix; else the same on its parent ⇒ parent; else null / none.
# The two image sources are only compared (`pair`), never returned.
_ROTATE_V2 = ELEMENTS["rotate_v2"]
_ROTATE_V2_PROBE = (
    " const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect();"
    " return { x: r.x, y: r.y, width: r.width, height: r.height }; };"
    " const own = (el) => {"
    " const m = /rotate\\((-?\\d+(?:\\.\\d+)?)deg\\)/.exec(String(el.getAttribute('style') || ''));"
    " if (m) return [Number(m[1]), 'inline'];"
    " const k = /^matrix\\(\\s*(-?[\\d.e+-]+)\\s*,\\s*(-?[\\d.e+-]+)/.exec(String(getComputedStyle(el).transform || ''));"
    " if (k) return [Math.atan2(Number(k[2]), Number(k[1])) * 180 / Math.PI, 'matrix'];"
    " return null; };"
    " const turn = (el) => { if (!el) return [null, 'none']; const a = own(el); if (a) return a;"
    " const p = el.parentElement ? own(el.parentElement) : null; return p ? [p[0], 'parent'] : [null, 'none']; };"
    " const nat = (el) => el ? el.naturalWidth : null;"
    f" const hd = first({json.dumps(_ROTATE_V2['handle'])});"
    f" const ii = first({json.dumps(_ROTATE_V2['inner'])});"
    f" const oo = first({json.dumps(_ROTATE_V2['outer'])});"
    " const ri = turn(ii); const ro = turn(oo);"
    " const si = ii ? String(ii.currentSrc || '') : ''; const so = oo ? String(oo.currentSrc || '') : '';"
    " out.probe = {"
    " track: box(hd ? hd.parentElement : null), inner: box(ii), outer: box(oo),"
    " in_nat: nat(ii), out_nat: nat(oo), rot_in: ri[0], rot_out: ro[0], rsrc_in: ri[1], rsrc_out: ro[1],"
    " pair: (ii && oo) ? (si !== '' && si === so ? 'same' : 'ok') : null };"
)
