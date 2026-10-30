"""Conservative, explainable page classifier. Calibration required on real samples."""
import re
import subprocess
import unicodedata
from PIL import Image
import pymupdf as fitz


CLASSIFICATION_RULE='v2'


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
    # A damaged title can lose the word 'requerimento'; require several printed form labels.
    structural=['data de nascimento','estado civil','endereco','renda','cadastro','cpf','nome','imovel']
    damaged_title=(re.search(r'isencao\s+(?:de|do)\s+iptu\s*2027\b',t)
                   and 'prefeitura' in t and len(front)>=4
                   and sum(label in t for label in structural)>=6)
    if (title or damaged_title) and len(front) >= 4:
        roles.append('inicio')
    if len(family) >= 3 and ('prefeitura' in t or 'imovel' in t):
        roles.append('continuacao')
    if 'assinatura' in t and 'requerente' in t and len(ending) >= 3 and 'iptu' in t:
        roles.append('encerramento')
    candidate = bool(roles) or ('iptu' in t and ('isencao' in t or 'requerimento' in t)) or len(family) >= 2
    return dict(roles=roles, candidate=candidate,
                evidence=dict(inicio=front, continuacao=family, encerramento=ending,
                              exercicios=sorted({int(y) for y in re.findall(r'\b(?:iptu|exercicio(?: fiscal)?|requerimento)\s*(?:do\s+exercicio\s*)?(?:de\s*)?[:/\-–]?\s*(20\d{2})\b',t)}),
                              exercicio_regra=CLASSIFICATION_RULE))


def reliable_native_body(page,text):
    """Only bypass OCR for substantial digital text without a large embedded scan."""
    if text.count('\ufffd') or text.count('\x00'):return False
    rect=page.rect
    if not rect.get_area():return False
    if any(fitz.Rect(image['bbox']).get_area()>.05*rect.get_area() for image in page.get_image_info()):
        return False
    words=[word for word in page.get_text('words')
           if .06*rect.width<(word[0]+word[2])/2<.94*rect.width
           and .12*rect.height<(word[1]+word[3])/2<.88*rect.height]
    if len(words)<40 or sum(len(word[4]) for word in words)<240:return False
    tokens=re.findall(r'\b[a-z]+\b',normalize(' '.join(word[4] for word in words)))
    common={'de','do','da','dos','das','e','o','a','os','as','em','para','com','que','por','no','na','ao','um','uma','se','sao'}
    return len(set(tokens)&common)>=4 and sum(token in common for token in tokens)>=8


def read_page(page, workdir, index, dpi=145):
    text = page.get_text()
    if len(text.strip()) >= 120:
        result = classify(text)
        # Native text can be only a SEI header: still inspect the scanned image.
        if result['roles']:
            return text, result, 0
        if not result['candidate'] and reliable_native_body(page,text):
            result['evidence']['leitura']='texto_digital_completo'
            return text,result,0
    pix = page.get_pixmap(dpi=dpi)
    image = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
    path = workdir / f'page-{index}.png'
    image.save(path,compress_level=1)
    orientation = 0
    try:
        osd = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'osd', '--psm', '0'],
                             capture_output=True, text=True, timeout=25)
        match = re.search(r'Rotate: (90|180|270)', osd.stdout)
        if match:
            orientation = int(match.group(1))
            image = image.rotate(-orientation, expand=True)
            image.save(path,compress_level=1)
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
