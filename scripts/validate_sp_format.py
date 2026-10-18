"""Run the installed SP format validator in Node, without launching or importing into SP.

This optional developer check requires Node and a Super Productivity 19.2.0 app.asar.
Only generated validation modules are extracted into a disposable directory.
No application profile or persistence service is opened.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import subprocess
import tempfile
from pathlib import Path

RUNNER = r"""
import fs from 'node:fs';
globalThis.window = globalThis;
globalThis.location = {
  host:'localhost', protocol:'http:', href:'http://localhost/', pathname:'/', hash:'', search:''
};
Object.defineProperty(globalThis, 'navigator', {
  value:{userAgent:'Offline format validation', platform:'Linux', language:'en'}
});
globalThis.document = {documentElement:{style:{}}, createElement:()=>({style:{}})};
globalThis.localStorage = {getItem:()=>null, setItem:()=>{}};
globalThis.fetch = () => { throw new Error('Network disabled during format validation'); };
let suppressedLogMessages = 0;
console.log = console.warn = console.error = () => { suppressedLogMessages++; };
try {
  const {validateFull} = await import('./' + process.argv[3]);
  const backup = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const result = validateFull(backup.data ?? backup);
  process.stdout.write(JSON.stringify({
    isValid:result.isValid,
    errors:result.typiaResult.errors?.map(e=>({path:e.path, expected:e.expected})),
    hasCrossModelError:Boolean(result.crossModelError),
    suppressedLogMessages
  }) + '\n');
  process.exitCode = result.isValid ? 0 : 1;
} catch (error) {
  process.stdout.write(JSON.stringify({isValid:false, validatorErrorType:error.name}) + '\n');
  process.exitCode = 2;
}
"""


def validate(asar: Path, backup: Path) -> int:
    with (
        asar.open("rb") as archive,
        tempfile.TemporaryDirectory(prefix="mstodo-extract-format-") as temp,
    ):
        prefix = archive.read(16)
        header = json.loads(archive.read(struct.unpack("<I", prefix[12:16])[0]))
        entries = header["files"][".tmp"]["files"]["angular-dist"]["files"]["browser"]["files"]
        data_start = 8 + struct.unpack("<I", prefix[4:8])[0]

        def read(name: str) -> bytes:
            meta = entries[name]
            archive.seek(data_start + int(meta["offset"]))
            return archive.read(meta["size"])

        validators = [
            name
            for name, meta in entries.items()
            if name.endswith(".js")
            and "offset" in meta
            and re.search(rb"export\{[^}]*\bas validateFull\b", read(name))
        ]
        if len(validators) != 1:
            raise ValueError("Could not identify the SP 19.2.0 format validator")
        root = Path(temp)
        pending, visited = [validators[0]], set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            contents = read(name)
            (root / name).write_bytes(contents)
            pending.extend(
                re.findall(
                    r"""(?:from|import)\s*(?:\(\s*)?["']\./([^"']+\.js)["']""",
                    contents.decode(),
                )
            )
        (root / "package.json").write_text('{"type":"module"}\n')
        (root / "validate.mjs").write_text(RUNNER)
        result = subprocess.run(
            ["node", str(root / "validate.mjs"), str(backup.resolve()), validators[0]],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        # Native logging is suppressed; reports deliberately exclude erroneous data values.
        print(result.stdout.strip() or '{"isValid":false,"validatorErrorType":"ProcessError"}')
        return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument(
        "--asar", type=Path, default=Path("/opt/Super Productivity/resources/app.asar")
    )
    args = parser.parse_args()
    try:
        return validate(args.asar, args.backup)
    except (OSError, ValueError, KeyError, struct.error, subprocess.TimeoutExpired) as error:
        print(json.dumps({"isValid": False, "validatorErrorType": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
