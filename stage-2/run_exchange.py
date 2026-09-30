"""Fetch pinned inputs, normalize, calculate, and export foreign results with source URLs."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

STAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE / 'tool'))
import settle


def read_manifest():
    return json.loads((STAGE / 'provenance.json').read_text(encoding='utf-8'))


def sources_ready(sources, manifest, fetch=False):
    for name, entry in manifest.items():
        repo = sources / entry['folder']
        if not repo.exists():
            if not fetch:
                raise RuntimeError(f'{name}: исходники отсутствуют. Сначала запустите run_exchange.py --fetch')
            repo.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'clone', '--no-checkout', entry['repository'], str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', entry['revision']], check=True)
        actual = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != entry['revision']:
            raise RuntimeError(f'{name}: другая ревизия, существующий каталог оставлен без изменений')
        verify_hashes(repo, entry)


def verify_hashes(repo, entry):
    for filename, expected in entry['files'].items():
        digest = hashlib.sha256((repo / filename).read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError(f'Изменён источник: {filename}')


def source_url(value, sources, manifest):
    """Convert local citations to immutable web URLs, preserving exact source anchors."""
    if not isinstance(value, str):
        return value
    file, _, fragment = value.partition('#')
    for name, entry in manifest.items():
        repo = (sources / entry['folder']).resolve()
        try:
            relative = Path(file).relative_to(repo)
        except ValueError:
            continue
        if name == 'gist':
            url = entry['source_url'] + '/' + entry['revision']
            # Gist prefixes user-supplied anchors with user-content-.
            anchor = 'user-content-' + fragment if fragment else 'file-report-md'
        else:
            url = entry['source_url'] + '/blob/' + entry['revision'] + '/' + quote(relative.as_posix())
            anchor = fragment
        return url + ('#' + quote(anchor) if anchor else '')
    return value


def web_citations(value, sources, manifest):
    if isinstance(value, dict):
        return {key: web_citations(item, sources, manifest) for key, item in value.items()}
    if isinstance(value, list):
        return [web_citations(item, sources, manifest) for item in value]
    return source_url(value, sources, manifest)


def calculate(source, output, sources, manifest, scenario='confirmed', normalized=None):
    output.mkdir(parents=True, exist_ok=True)
    if normalized is None:
        normalized = output / 'normalized.json'
        subprocess.run([sys.executable, str(STAGE / 'tool/normalize.py'), str(source), '--output', str(normalized)], check=True, capture_output=True, text=True)
    subprocess.run([sys.executable, str(STAGE / 'tool/settle.py'), str(normalized), '--output', str(output), '--scenario', scenario], check=True, capture_output=True, text=True)
    result = web_citations(json.loads((output / 'result.json').read_text()), sources, manifest)
    assert all(result['checks'].values())
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'report.md').write_text(settle.render(result, output), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true', help='Клонировать отсутствующие pinned репозитории')
    parser.add_argument('--sources-dir', type=Path, default=STAGE / '.cache/sources')
    parser.add_argument('--output', type=Path, default=STAGE / '.runs')
    args = parser.parse_args()
    sources = args.sources_dir.resolve()
    output = args.output.resolve()
    manifest = read_manifest()
    sources_ready(sources, manifest, args.fetch)
    runs = []
    for name, entry in manifest.items():
        result = calculate(sources / entry['folder'] / entry['input'], output / name, sources, manifest)
        runs.append(dict(dataset=name, checks=result['checks'], final=result['final_settlement_ready']))
        print(name + ': PASS; final=' + str(result['final_settlement_ready']))
    result = calculate(None, output / 'tbilisi-equal-provisional', sources, manifest, 'equal-provisional', output / 'tbilisi/normalized.json')
    runs.append(dict(dataset='tbilisi-equal-provisional', checks=result['checks'], final=result['final_settlement_ready']))
    # Verify original bytes again after all computations.
    for entry in manifest.values():
        verify_hashes(sources / entry['folder'], entry)
    audit = dict(runs=runs, source_hashes_unchanged=True, source_data_edits=0, normalized_data_manual_edits=0, ocr_manual_corrections=0)
    (output / 'run-audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('SOURCE HASHES: PASS. Собственная поездка проверяется тестами; её ответы не экспортируются.')


if __name__ == '__main__':
    main()
