#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Весовой конвейер с двойным замком · singulyar-llm#12 · Волна ×3 (W3-F1).
Замок 1: WEIGHTS.MANIFEST.json {file, bytes, sha256} (register — append-only, новая версия
весов = новая запись) + парсер GGUF-заголовка: усечение/подмена ловятся до загрузки.
Замок 2: engine-lock.json — commit-SHA llama.cpp и hash wasm-сборки (--strict — красный).
sums — строки SHA256SUMS для Release; init создаёт каркасы, не перезаписывая.
"""
import argparse, hashlib, json, re, struct, sys, tempfile
from pathlib import Path
MAGIC, KNOWN_VERSIONS = b'GGUF', (2, 3)
SCALAR = {0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i', 6: '<f', 7: '<B', 10: '<Q', 11: '<q', 12: '<d'}
DEFAULT_ALIGN, SANITY = 32, 1_000_000  # выравнивание по умолчанию; санитарный предел счётчиков
MIN_BPE = 8  # нижняя граница плотности: не плотнее 1/8 байта на элемент (реальный минимум ~0.33)
class GGUFError(Exception): pass
class Reader:
    def __init__(self, data):
        self.d, self.p = data, 0
    def take(self, n):
        if self.p + n > len(self.d):
            raise GGUFError('обрыв файла: с offset %d нужно %d Б, осталось %d' % (self.p, n, len(self.d) - self.p))
        b = self.d[self.p:self.p + n]; self.p += n
        return b
    def num(self, f):
        return struct.unpack(f, self.take(struct.calcsize(f)))[0]
    def string(self):
        n = self.num('<Q')
        if n > SANITY: raise GGUFError('строка слишком длинная: %d' % n)
        return self.take(n).decode('utf-8', 'replace')
    def value(self, t):
        if t == 8: return self.string()
        if t == 9:
            et, cnt = self.num('<I'), self.num('<Q')
            if cnt > SANITY: raise GGUFError('массив слишком длинный: %d' % cnt)
            return [self.value(et) for _ in range(cnt)]
        if t not in SCALAR: raise GGUFError('неизвестный тип значения %d' % t)
        return struct.unpack(SCALAR[t], self.take(struct.calcsize(SCALAR[t])))[0]
def parse_gguf(data):
    r = Reader(data)
    if r.take(4) != MAGIC: raise GGUFError('магия не GGUF — файл битый или подменён')
    ver = r.num('<I')
    if ver not in KNOWN_VERSIONS: raise GGUFError('версия %d вне известных %s' % (ver, KNOWN_VERSIONS))
    n_t, n_kv = r.num('<Q'), r.num('<Q')
    if n_t > SANITY or n_kv > SANITY: raise GGUFError('заголовок неправдоподобен: тензоров %d, K/V %d' % (n_t, n_kv))
    kv = {}
    for _ in range(n_kv):
        k = r.string()
        kv[k] = r.value(r.num('<I'))
    align = kv.get('general.alignment')
    align = align if isinstance(align, int) and 0 < align <= 65536 else DEFAULT_ALIGN
    tensors = []
    for _ in range(n_t):
        name, nd = r.string(), r.num('<I')
        if not 1 <= nd <= 8: raise GGUFError('тензор %r: размерностей %d, ожидалось 1..8' % (name, nd))
        dims = [r.num('<Q') for _ in range(nd)]
        if 0 in dims: raise GGUFError('тензор %r: нулевая размерность' % name)
        tensors.append({'name': name, 'dims': dims, 'type': r.num('<I'), 'offset': r.num('<Q')})
    start = (r.p + align - 1) // align * align
    for t in tensors:
        prod = 1
        for d in t['dims']:
            prod *= d
        if start + t['offset'] + prod // MIN_BPE > len(data):
            raise GGUFError('тензор %r выходит за файл (усечение?) при размере %d' % (t['name'], len(data)))
    return {'version': ver, 'kv': kv, 'tensors': tensors, 'data_start': start}
def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()
def load_manifest(root):
    p = root / 'WEIGHTS.MANIFEST.json'
    return (json.loads(p.read_text(encoding='utf-8')) if p.exists() else None), p
def cmd_init(root):
    for name, obj in (
        ('WEIGHTS.MANIFEST.json', {'schema': 'wave3/weights-manifest@1', 'parts': [],
                                   'note': '(заполняется скриптом при первом прогоне: register <файл>)'}),
        ('engine-lock.json', {'engine': 'llama.cpp', 'commit': '', 'wasm_sha256': '', 'pinned_at': '',
                              'note': '(заполняется при первом прогоне: commit-SHA llama.cpp и hash wasm-сборки)'}),
    ):
        p = root / name
        if p.exists(): print('уже есть %s — не трогаю (append-only)' % name)
        else:
            p.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
            print('создан %s' % p)
    return 0
def cmd_register(root, files):
    man, mp = load_manifest(root)
    if man is None: print('манифеста нет — сначала: weights_gate.py init'); return 1
    parts, changed, rc = man.setdefault('parts', []), False, 0
    for f in files:
        p = Path(f)
        if not p.is_file():
            print('нет файла: %s' % f); rc = 1; continue
        try:
            rel = p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            print('%s вне корня репо — отказываюсь' % f); rc = 1; continue
        size, sha = p.stat().st_size, sha256_file(p)
        dup = next((e for e in parts if e.get('file') == rel), None)
        if dup and dup.get('sha256') == sha and dup.get('bytes') == size:
            print('уже зарегистрирован: %s (запись не переписана)' % rel)
        elif dup:
            print('КОНФЛИКТ: %s уже в манифесте с другим хешем; новая версия весов = новая запись' % rel); rc = 1
        else:
            parts.append({'file': rel, 'bytes': size, 'sha256': sha}); changed = True
            print('зарегистрирован: %s (%d Б, sha256 %s…)' % (rel, size, sha[:12]))
    if changed: mp.write_text(json.dumps(man, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return rc
def cmd_manifest(root):
    """Замок 1: sha256-сверка частей + валидация GGUF-заголовка каждой .gguf-части."""
    man, _ = load_manifest(root)
    if man is None: print('манифеста нет — сначала init'); return 1
    parts = man.get('parts', [])
    if not parts: print('(манифест пуст — части регистрируются register-ом при первом прогоне; пока зелёный)'); return 0
    fails = 0
    for e in parts:
        p = root / e['file']
        size = p.stat().st_size if p.is_file() else None
        if size != e.get('bytes') or sha256_file(p) != e.get('sha256'):
            print('FAIL %s — файла нет, размер или sha256 не совпали (подмена байта?)' % e['file']); fails += 1; continue
        head = 'sha256 ok'
        if e['file'].endswith('.gguf'):
            try:
                i = parse_gguf(p.read_bytes())
                head = 'GGUF v%d, K/V %d, тензоров %d' % (i['version'], len(i['kv']), len(i['tensors']))
            except GGUFError as err:
                print('FAIL %s — заголовок: %s' % (e['file'], err)); fails += 1; continue
        print('OK   %s — %s' % (e['file'], head))
    print('итог: частей %d, провалов %d' % (len(parts), fails))
    return 1 if fails else 0
def cmd_engine_lock(root, strict=False):
    p = root / 'engine-lock.json'
    if not p.exists(): print('engine-lock.json нет — init создаст каркас'); return 1 if strict else 0
    d = json.loads(p.read_text(encoding='utf-8'))
    commit, wasm = str(d.get('commit', '')), str(d.get('wasm_sha256', ''))
    if not commit: print('пин движка не выставлен (commit пуст) — заполните engine-lock.json при первом прогоне'); return 1 if strict else 0
    if not re.fullmatch(r'[0-9a-fA-F]{40}', commit) or (wasm and not re.fullmatch(r'[0-9a-fA-F]{64}', wasm)):
        print('FAIL engine-lock: commit не 40-симв. SHA или wasm_sha256 не 64-симв. sha256'); return 1
    print('пин движка: %s (wasm %s)' % (commit[:12], (wasm[:12] + '…') if wasm else 'не задан'))
    return 0
def cmd_sums(root):
    parts = (load_manifest(root)[0] or {}).get('parts', [])
    for e in parts: print('%s  %s' % (e.get('sha256', ''), e['file']))
    if not parts: print('(пусто — после register здесь будут строки SHA256SUMS для Release)')
    return 0
def synth_gguf():
    import io; b = io.BytesIO()
    def pv(t, v):
        if t == 8:
            b = v.encode()
            return struct.pack('<Q', len(b)) + b
        return struct.pack(SCALAR[t], v)
    b.write(MAGIC); b.write(struct.pack('<I', 3))
    b.write(struct.pack('<Q', 2)); b.write(struct.pack('<Q', 2))
    for k, t, v in (('general.alignment', 4, 32), ('selftest.model', 8, 'wave3-selftest')):
        kb = k.encode()
        b.write(struct.pack('<Q', len(kb)) + kb + struct.pack('<I', t) + pv(t, v))
    for name, off in ((b't0', 0), (b't1', 16)):
        b.write(struct.pack('<Q', len(name)) + name + struct.pack('<I', 2) + struct.pack('<QQ', 2, 2) + struct.pack('<I', 0) + struct.pack('<Q', off))
    body = b.getvalue()
    start = (len(body) + DEFAULT_ALIGN - 1) // DEFAULT_ALIGN * DEFAULT_ALIGN
    return body + b'\x00' * (start - len(body)) + b'\x01' * 32
def selftest():
    fails, g = [], synth_gguf()
    for label, blob, frag in (('целый', g, None), ('усечение 50%', g[:len(g) // 2], 'обрыв|выходит'), ('магия', b'XGUF' + g[4:], 'магия')):
        try:
            parse_gguf(blob)
            if frag: fails.append('%s не пойман' % label)
        except GGUFError as e:
            if not frag or not re.search(frag, str(e)): fails.append('%s: неожиданный диагноз: %s' % (label, e))
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = root / 'СБОРКА' / 'part-000.gguf'; f.parent.mkdir(); f.write_bytes(g)
        if cmd_init(root) or cmd_register(root, [f]): fails.append('init/register отказали')
        if cmd_manifest(root): fails.append('неизменённые веса красные')
        data = bytearray(f.read_bytes()); data[-1] ^= 0xFF; f.write_bytes(bytes(data))
        if cmd_manifest(root) != 1: fails.append('подмена байта не поймана')
        f.write_bytes(g[:len(g) // 2])
        if cmd_manifest(root) != 1: fails.append('усечение не поймано')
        if cmd_engine_lock(root, strict=True) != 1: fails.append('пустой пин должен быть красным в strict')
    print('selftest: %s' % ('OK — все ловушки сработали' if not fails else 'ПРОВАЛ: ' + '; '.join(fails)))
    return 1 if fails else 0
def main(argv=None):
    ap = argparse.ArgumentParser(description='Весовой конвейер с двойным замком (singulyar-llm#12)')
    ap.add_argument('--selftest', action='store_true', help='самотест на синтетическом GGUF')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('init'); sub.add_parser('manifest'); sub.add_parser('sums')
    r = sub.add_parser('register'); r.add_argument('files', nargs='+')
    e = sub.add_parser('engine-lock'); e.add_argument('--strict', action='store_true')
    a = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    if a.selftest: return selftest()
    if a.cmd == 'register': return cmd_register(root, a.files)
    if a.cmd == 'engine-lock': return cmd_engine_lock(root, a.strict)
    return {'init': cmd_init, 'manifest': cmd_manifest, 'sums': cmd_sums}.get(a.cmd, lambda x: (ap.print_help(), 0)[1])(root)
if __name__ == '__main__':
    sys.exit(main())
