import tarfile
import zipfile
from pathlib import Path

root=Path(__file__).resolve().parents[1]
excluded={'.venv','.state','.build','.build-venv','__pycache__','.pytest_cache','.git','build','dist','bin','outputs'}
included={'hivemind','web','connectors','docs','scripts','tests','.github','downloads',
          'README.md','LICENSE','pyproject.toml','requirements.lock','.gitignore','.gitattributes'}
target=root.parent/'outputs'/'hivemind-release.tar.gz'
target.parent.mkdir(exist_ok=True)
with tarfile.open(target,'w:gz') as archive:
    for file in root.rglob('*'):
        relative=file.relative_to(root)
        if relative.parts[0] in included and file.is_file() and not file.is_symlink() and not any(part in excluded or part.endswith('.egg-info') for part in relative.parts):
            if file.suffix not in {'.pyc','.env','.db','.pem','.key'} and not file.name.startswith('.env'):
                archive.add(file,arcname=str(relative))
print(target)
source=target.with_name('HiveMind-Source.zip')
with zipfile.ZipFile(source,'w',zipfile.ZIP_DEFLATED) as archive:
    for file in root.rglob('*'):
        relative=file.relative_to(root)
        if relative.parts[0] in included and file.is_file() and not file.is_symlink() and not any(part in excluded | {'downloads'} or part.endswith('.egg-info') for part in relative.parts):
            if file.suffix not in {'.pyc','.db','.pem','.key'} and not file.name.startswith('.env'):
                archive.write(file,arcname=str(Path('hivemind')/relative))
print(source)
