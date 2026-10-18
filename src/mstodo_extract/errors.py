class ExportError(Exception):
    """An actionable error that can be printed without a traceback or credentials."""


class NullLinkedResourceError(ExportError):
    """Graph interrupted a task page while serializing a null linkedResources member."""
