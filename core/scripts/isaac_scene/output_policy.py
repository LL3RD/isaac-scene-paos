"""Local accidental-overwrite guards, not an OS sandbox or an untrusted-code boundary.

The caller owns a private task directory. A platform adapter must also constrain
inputs, transitive asset dependencies, process ownership and concurrent access.
"""
from pathlib import Path


def new_output(value, *, root=None):
    path = Path(value).expanduser()
    if path.is_symlink():
        raise ValueError("Output must not be a symbolic link")
    resolved = path.resolve()
    if root is not None:
        workspace = Path(root).expanduser().resolve()
        if workspace == Path(workspace.anchor) or workspace == Path.home().resolve():
            raise ValueError("Use a dedicated task output root")
        if resolved == workspace or workspace not in resolved.parents:
            raise ValueError("Output must be inside the configured task output root")
    if resolved.exists():
        raise ValueError("Output exists; choose a new revision path")
    return resolved


def check_cli_outputs(args, config):
    """Check explicit output arguments before any command executes.

    Helpers which derive additional outputs must check those at their write site.
    Shared catalog maintenance is a trusted local operation, not a service API.
    """
    runtime = config.get("runtime") or {}
    root = runtime.get("output_root")
    paths = []
    for field in ("output", "output_spec", "output_scene", "output_dir", "report", "report_output"):
        value = getattr(args, field, None)
        if value:
            paths.append(new_output(value, root=root))
    if len(paths) != len(set(paths)):
        raise ValueError("Output and report paths must be distinct")


def check_scene_outputs(value, config, owned=None):
    """A solver may refresh only files it created during this invocation."""
    root = (config.get("runtime") or {}).get("output_root")
    path = Path(value).expanduser().resolve()
    for target in (path, path.with_suffix(".scene_spec.json")):
        if owned is None or target not in owned:
            new_output(target, root=root)
    return path
