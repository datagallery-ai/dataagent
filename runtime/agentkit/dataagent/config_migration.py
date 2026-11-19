"""Plan a declared JSON configuration closure before creating any YAML output."""

import copy
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from dataagent.strict_json import read_json
from dataagent.strict_yaml import MAX_INTEGER, CoreLoader, parse_yaml, read_yaml


class ConfigDumper(yaml.SafeDumper):
    # Match the reader when deciding whether a string/key requires quotes.
    yaml_implicit_resolvers = CoreLoader.yaml_implicit_resolvers


def _string(dumper, value):
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
    )


ConfigDumper.add_representer(str, _string)


@dataclass
class Conversion:
    source: Path
    target: Path
    text: str
    action: str


def _equivalent(left, right):
    """JSON value equality includes types: true must never equal 1."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _equivalent(v, right[k]) for k, v in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(_equivalent(a, b) for a, b in zip(left, right))
    return left == right


def plan_migration(*, homes=(), plugins=(), files=()):
    inputs = {}
    notices = []
    visited_plugins = set()

    def validate(value, depth=0):
        if depth > 64:
            raise ValueError("configuration depth exceeds limit")
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_INTEGER:
            raise ValueError("integer outside safe range; quote it in the source JSON")
        if isinstance(value, dict):
            for item in value.values():
                validate(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                validate(item, depth + 1)

    def add(path):
        path = Path(os.path.abspath(path))
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            raise ValueError(f"{path}: expected a regular JSON configuration file")
        if path not in inputs:
            try:
                value = read_json(path)
                validate(value)
            except (ValueError, RecursionError):
                raise ValueError(
                    f"{path}: invalid JSON configuration or unsupported value"
                ) from None
            inputs[path] = copy.deepcopy(value)
        return inputs[path]

    def resource(root, relative):
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError(f"{root}: invalid declared configuration path")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"{root}: configuration reference escapes plugin or uses a link")
        if path.suffix in {".yaml", ".yml"}:
            read_yaml(path)
            return relative
        add(path)
        return str(Path(relative).with_suffix(".yaml"))

    def plugin(root):
        root = Path(os.path.abspath(root))
        if root in visited_plugins:
            return
        visited_plugins.add(root)
        manifest = root / ".plugin.json"
        if not manifest.exists():
            read_yaml(root / ".plugin.yaml")
            return
        data = add(manifest)
        if data.get("defaults_file") is not None:
            data["defaults_file"] = resource(root, data["defaults_file"])
        if "subagents" in data:
            if not isinstance(data["subagents"], list):
                raise ValueError(f"{manifest}: invalid subagent references")
            data["subagents"] = [resource(root, ref) for ref in data["subagents"]]
        if (root / ".mcp.json").exists():
            add(root / ".mcp.json")

    def product(path):
        data = add(path)
        section = data.get("plugins", {})
        if not isinstance(section, dict):
            raise ValueError(f"{path}: invalid plugin configuration")
        paths = section.get("paths", [])
        if not isinstance(paths, list):
            raise ValueError(f"{path}: invalid plugin paths")
        for ref in paths:
            if not isinstance(ref, str):
                raise ValueError(f"{path}: invalid plugin path")
            if "$env{" in ref:
                notices.append(f"{path}: unresolved plugin path; select it with --plugin")
            else:
                plugin(Path(path).absolute().parent / Path(ref).expanduser())

    for home in homes:
        home = Path(home).expanduser().absolute()
        if (home / "config.json").exists():
            product(home / "config.json")
        if (home / ".mcp.json").exists():
            add(home / ".mcp.json")
        if (home / "plugins").is_dir():
            for root in sorted((home / "plugins").iterdir()):
                if (root / ".plugin.json").exists():
                    plugin(root)
    for root in plugins:
        plugin(Path(root).expanduser())
    for path in files:
        path = Path(path).expanduser()
        if path.name == ".plugin.json":
            plugin(path.parent)
        else:
            product(path)
    plan = []
    for source, value in inputs.items():
        target = source.with_suffix(".yaml")
        text = yaml.dump(value, Dumper=ConfigDumper, sort_keys=False, allow_unicode=True)
        if not _equivalent(parse_yaml(text, target), value):
            raise ValueError(f"{source}: YAML round-trip mismatch")
        action = "create"
        if target.is_symlink():
            raise ValueError(f"{target}: output symlink conflict")
        if target.exists():
            if not _equivalent(read_yaml(target), value):
                raise ValueError(f"{target}: existing YAML conflict")
            action = "already_migrated"
        plan.append(Conversion(source, target, text, action))
    return plan, notices


def apply_migration(plan):
    created = []
    try:
        for item in plan:
            if item.action != "create":
                continue
            fd = os.open(item.target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stat = os.fstat(fd)
            created.append((item.target, stat.st_dev, stat.st_ino))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(item.text)
                stream.flush()
                os.fsync(stream.fileno())
    except BaseException:
        for path, device, inode in reversed(created):
            try:
                stat = path.lstat()
                if (stat.st_dev, stat.st_ino) == (device, inode):
                    path.unlink()
            except FileNotFoundError:
                pass
        raise


def add_arguments(commands):
    config = commands.add_parser("config", help="Configuration utilities")
    actions = config.add_subparsers(dest="config_action", required=True)
    migrate = actions.add_parser("migrate", help="Preview JSON to YAML migration")
    migrate.add_argument("--home", action="append", default=[])
    migrate.add_argument("--plugin", action="append", default=[])
    migrate.add_argument("--file", action="append", default=[])
    migrate.add_argument("--apply", action="store_true")


def run(args):
    from dataagent.bootstrap.paths import home_path

    homes = args.home or ([] if args.plugin or args.file else [home_path(Path.cwd())])
    try:
        plan, notices = plan_migration(homes=homes, plugins=args.plugin, files=args.file)
        if args.apply:
            apply_migration(plan)
        for item in plan:
            print(f"{item.action}: {item.source} -> {item.target}")
        for notice in notices:
            print(notice)
        print(
            f"{len(plan)} configuration files; "
            + (
                "applied. Restart the application."
                if args.apply
                else "preview only; use --apply to write."
            )
        )
        return 0
    except (ValueError, OSError) as error:
        # OSError messages can contain paths but never configuration source text.
        print(f"Migration failed: {error}")
        return 1
