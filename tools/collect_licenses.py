"""Copy original dependency notices into redistributable application bundles."""
import importlib.metadata
from pathlib import Path
import shutil
import sys
import sysconfig


def collect(names, target, python=False):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        distribution = importlib.metadata.distribution(name)
        for file in distribution.files or ():
            parts = Path(file).parts
            if (not parts or not parts[0].endswith('.dist-info') or
                    not any(word in Path(file).name.casefold() for word in ('license', 'copying', 'notice', 'authors'))):
                continue
            source = Path(distribution.locate_file(file))
            if source.is_file():
                destination = target / name / Path(*parts[1:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    if python:
        source = next((p for p in (Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt',
                                   Path(sys.base_prefix) / 'LICENSE.txt') if p.is_file()), None)
        if source is None:
            raise RuntimeError('构建环境缺少 Python LICENSE.txt')
        shutil.copy2(source, target / 'Python-LICENSE.txt')
    return target
