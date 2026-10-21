"""Generate PyInstaller's Windows version resource without importing the GUI."""
import argparse
import ast
from pathlib import Path
import re

parser = argparse.ArgumentParser()
parser.add_argument("version")
parser.add_argument("output", type=Path)
parser.add_argument("--app-source", type=Path)
args = parser.parse_args()
if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", args.version):
    parser.error("Expected a semantic version, for example 1.0.0")
numbers = tuple(int(part) for part in args.version.split("-")[0].split(".")) + (0,)
if any(part > 65535 for part in numbers):
    parser.error("Windows version components must not exceed 65535")
if args.app_source:
    module = ast.parse(args.app_source.read_text(encoding="utf-8-sig"))
    declared = [node.value.value for node in module.body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets)]
    if declared != [args.version]:
        parser.error("The release version must match the APP_VERSION constant in app.py")
content = f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r}, mask=0x3f,
    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'Soda Music Downloader'),
    StringStruct('FileVersion', {args.version!r}),
    StringStruct('InternalName', 'SodaMusicDownloader'),
    StringStruct('OriginalFilename', 'SodaMusicDownloader.exe'),
    StringStruct('ProductName', 'Soda Music Downloader'),
    StringStruct('ProductVersion', {args.version!r})
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
'''
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(content, encoding="utf-8")
