"""Build a portable, install-free copy of the tool with PyInstaller.

Usage (inside the project's virtual environment):
    python build_exe.py

The result is dist/KeyframeTool/: zip that folder and hand it out. Double-clicking
KeyframeTool.exe (KeyframeTool on macOS/Linux) starts the tool and opens the browser;
screenshots and the work folder are kept next to the executable.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = 'KeyframeTool'
# Modules imported inside functions are listed explicitly so the bundle never misses them.
HIDDEN = [
    'detection',
    'collage_engine',
    'justified_layout',
    'workspace_store',
    'storage_manager',
    'runtime_compat',
    'multipart',
    'PIL.ImageDraw',
    'PIL.ImageFilter',
    'PIL.ImageFont',
]


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print('Installing PyInstaller ...')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', 'pyinstaller'])
    for folder in ('build', 'dist'):
        shutil.rmtree(ROOT / folder, ignore_errors=True)
    command = [
        sys.executable,
        '-m',
        'PyInstaller',
        '--noconfirm',
        '--clean',
        '--onedir',
        '--console',
        '--name',
        NAME,
        '--distpath',
        str(ROOT / 'dist'),
        '--workpath',
        str(ROOT / 'build'),
        '--specpath',
        str(ROOT / 'build'),
        '--add-data',
        f"{ROOT / 'static'}{os.pathsep}static",
        '--add-data',
        f"{ROOT / 'models'}{os.pathsep}models",
        '--collect-submodules',
        'uvicorn',
    ]
    for module in HIDDEN:
        command += ['--hidden-import', module]
    command.append(str(ROOT / 'launcher.py'))
    subprocess.check_call(command, cwd=ROOT)
    target = ROOT / 'dist' / NAME
    (target / 'fonts').mkdir(exist_ok=True)
    shutil.copy(ROOT / '使用说明.txt', target / '使用说明.txt')
    (target / 'fonts' / '放字体文件到这里.txt').write_text(
        '拼图标题和标注需要中文字体。Windows 自带微软雅黑，一般无需操作；\n'
        '如果中文显示为方框，把 .ttf / .otf / .ttc 字体文件放进这个文件夹即可。\n',
        encoding='utf-8',
    )
    print(f'\nDone: {target}\nZip this folder to share it. Run {NAME}.exe inside it to start the tool.')


if __name__ == '__main__':
    main()
