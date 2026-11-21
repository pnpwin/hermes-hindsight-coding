from .provider import CodingAgentsMemoryProvider


def register(ctx):
    ctx.register_memory_provider(CodingAgentsMemoryProvider())
