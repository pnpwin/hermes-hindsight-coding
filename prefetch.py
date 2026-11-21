"""Translate immutable Hermes bindings to the Coding Agents wire descriptor."""


def route_descriptor(lease):
    binding = lease.binding
    return {
        'active': True,
        'bankId': binding.bank_id,
        'apiUrl': binding.api_url,
        'cwd': str(lease.cwd),
        'startCwd': binding.start_cwd,
        'projectRoot': binding.project_root,
        'projectIdentity': binding.project_identity,
        'configIdentity': binding.config_identity,
        'retainSessions': binding.retain_sessions,
        'seedMessage': binding.seed_message,
    }


def context_parts(result):
    """Only dynamic context enters Hermes; its static system prompt remains unchanged."""
    return '\n\n'.join(part.strip() for part in (result['additionalContext'], result['context']) if part.strip())
