from providers import register_provider
from providers.base import ProviderProfile

nexosai = ProviderProfile(
    name="nexosai",
    aliases=("nexos","nexosai",),
    display_name="Nexos.ai",
    description="Nexos.ai AI gateway (unified, multi-provider model access)",
    signup_url="https://nexos.ai/pricing/",
    env_vars=("NEXOS_API_KEY",),
    base_url="https://api.nexos.ai/v1",
)

register_provider(nexosai)
