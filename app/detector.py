"""Conservative, explainable page classifier. Calibration required on real samples."""
import re
import subprocess
import unicodedata
from PIL import Image
import pymupdf as fitz


def normalize(text):
    return re.sub(r'\s+', ' ', ''.join(c for c in unicodedata.normalize('NFKD', text.lower())
                                     if not unicodedata.combining(c)))


def classify(text):
    t = normalize(text)
    front = [s for s in ['requerimento', 'isencao', 'iptu', 'nome', 'imovel'] if s in t]
    family = [s for s in ['conjuge', 'dependente', 'trabalha', 'socio de empresa',
                         'prestacao continuada'] if s in t]
    ending = [s for s in ['assinatura', 'requerente', 'presente requerimento',
                         'lei complementar', 'observacoes'] if s in t]
    roles = []
    title = re.search(r'requerimento.{0,12}isencao.{0,12}iptu', t)
    if title and len(front) >= 4:
        roles.append('inicio')
    if len(family) >= 3 and ('prefeitura' in t or 'imovel' in t):
        roles.append('continuacao')
    if 'assinatura' in t and 'requerente' in t and len(ending) >= 3 and 'iptu' in t:
        roles.append('encerramento')
    candidate = bool(roles) or ('iptu' in t and ('isencao' in t or 'requerimento' in t)) or len(family) >= 2
    return dict(roles=roles, candidate=candidate,
                evidence=dict(inicio=front, continuacao=family, encerramento=ending))


def read_page(page, workdir, index, dpi=145):
    text = page.get_text()
    if len(text.strip()) >= 120:
        result = classify(text)
        # Native text can be only a SEI header: still inspect the scanned image.
        if result['roles']:
            return text, result, 0
    pix = page.get_pixmap(dpi=dpi)
    image = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
    path = workdir / f'page-{index}.png'
    image.save(path)
    orientation = 0
    try:
        osd = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'osd', '--psm', '0'],
                             capture_output=True, text=True, timeout=25)
        match = re.search(r'Rotate: (90|180|270)', osd.stdout)
        if match:
            orientation = int(match.group(1))
            image = image.rotate(-orientation, expand=True)
            image.save(path)
    except subprocess.TimeoutExpired:
        pass
    try:
        run = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'por', '--psm', '11'],
                             capture_output=True, text=True, timeout=90)
        if run.returncode:
            raise RuntimeError('Falha no Tesseract ao ler a página.')
        text = text + '\n' + run.stdout
        return text, classify(text), orientation
    finally:
        path.unlink(missing_ok=True)


def assemble(pages):
    selected = [p for p in pages if p['roles']]
    starts = [p for p in selected if 'inicio' in p['roles']]
    continuations = [p for p in selected if 'continuacao' in p['roles']]
    ends = [p for p in selected if 'encerramento' in p['roles']]
    ambiguous = any(p['candidate'] and not p['roles'] for p in pages)
    if not selected:
        return ('revisao' if ambiguous else 'nao_encontrado'), []
    # Never silently combine duplicate forms or assume all arbitrary layouts are complete.
    if len(starts) > 1 or len(continuations) > 1 or len(ends) > 1 or ambiguous:
        status = 'revisao'
    elif not starts or not continuations or not ends:
        status = 'incompleto'
    elif len(selected) in (2, 3):
        status = 'completo_provavel'
    else:
        status = 'revisao'
    priority = lambda p: (0 if 'inicio' in p['roles'] else 1 if 'continuacao' in p['roles'] else 2,
                          p['file_index'], p['page'])
    return status, sorted(selected, key=priority)
