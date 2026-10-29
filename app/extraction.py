"""Local OCR + geometric label extraction. No external AI service or invented values."""
import csv
import io
import json
import re
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pymupdf as fitz

# Stable field IDs, labels and types; unavailable/uncertain values stay null.
FIELDS = {
 'requerente.nome': ('Nome', 'text'),
 'requerente.rg': ('RG', 'text'),
 'requerente.cpf': ('CPF', 'cpf'),
 'requerente.data_nascimento': ('Data de nascimento', 'date'),
 'requerente.telefone': ('Telefone', 'phone'),
 'requerente.email': ('E-mail', 'email'),
 'requerente.estado_civil': ('Estado civil', 'choice'),
 'requerente.primeira_isencao': ('Primeira isenção', 'choice'),
 'requerente.pessoa_com_deficiencia': ('Pessoa com deficiência', 'choice'),
 'endereco.logradouro': ('Logradouro', 'text'),
 'endereco.numero': ('Número', 'text'),
 'endereco.complemento': ('Complemento', 'text'),
 'endereco.bairro': ('Bairro', 'text'),
 'endereco.cep': ('CEP', 'cep'),
 'imovel.cadastro': ('Cadastro do imóvel', 'text'),
 'imovel.tipo': ('Tipo do imóvel', 'text'),
 'imovel.tipo_escritura': ('Tipo de escritura', 'text'),
 'imovel.usufrutuario': ('Usufrutuário', 'choice'),
 'imovel.reside_no_imovel': ('Beneficiário reside no imóvel', 'choice'),
 'imovel.ponto_comercial': ('Ponto comercial', 'choice'),
 'imovel.possui_edicula': ('Possui edícula', 'choice'),
 'imovel.edicula_habitada': ('Edícula habitada', 'choice'),
 'imovel.edicula_alugada': ('Edícula alugada', 'choice'),
 'imovel.aluguel_edicula': ('Valor do aluguel da edícula', 'money'),
 'imovel.residente_edicula': ('Nome do residente da edícula', 'text'),
 'imovel.rg_residente_edicula': ('RG do residente da edícula', 'text'),
 'imovel.cpf_residente_edicula': ('CPF do residente da edícula', 'cpf'),
 'imovel.possui_outro': ('Possui outro imóvel', 'choice'),
 'imovel.cadastro_outro': ('Cadastro de outro imóvel', 'text'),
 'beneficios.recebe': ('Recebe benefício', 'choice'),
 'beneficios.tipo': ('Tipo de benefício', 'choice'),
 'beneficios.aposentado': ('É aposentado', 'choice'),
 'beneficios.aposentadoria_numero': ('Número da aposentadoria/benefício', 'text'),
 'beneficios.aposentadoria_valor': ('Valor da aposentadoria/benefício', 'money'),
 'beneficios.aposentadoria_orgao': ('Órgão do benefício', 'text'),
 'beneficios.pensionista': ('É pensionista', 'choice'),
 'beneficios.pensao_numero': ('Número da pensão', 'text'),
 'beneficios.pensao_valor': ('Valor da pensão', 'money'),
 'beneficios.pensao_orgao': ('Órgão da pensão', 'text'),
 'beneficios.pensao_data': ('Data de concessão da pensão', 'date'),
 'beneficios.outras_rendas': ('Possui outras fontes de renda', 'choice'),
 'atividade.socio_empresa': ('Sócio de empresa', 'choice'),
 'atividade.autonomo': ('Trabalha como autônomo', 'choice'),
 'atividade.registrado': ('Trabalha registrado', 'choice'),
 'conjuge.nome': ('Nome do cônjuge/companheiro', 'text'),
 'conjuge.rg': ('RG do cônjuge/companheiro', 'text'),
 'conjuge.cpf': ('CPF do cônjuge/companheiro', 'cpf'),
 'conjuge.beneficiario': ('Cônjuge aposentado/pensionista/BPC', 'choice'),
 'conjuge.beneficio_numero': ('Número do benefício do cônjuge', 'text'),
 'conjuge.beneficio_valor': ('Valor do benefício do cônjuge', 'money'),
 'conjuge.socio_empresa': ('Cônjuge sócio de empresa', 'choice'),
 'conjuge.outras_rendas': ('Cônjuge possui outras rendas', 'choice'),
 'conjuge.possui_outro_imovel': ('Cônjuge possui outro imóvel', 'choice'),
 'dependente.possui_deficiencia': ('Tem dependente com deficiência', 'choice'),
 'dependente.nome': ('Nome do dependente', 'text'),
 'dependente.parentesco': ('Parentesco', 'text'),
 'dependente.rg': ('RG do dependente', 'text'),
 'dependente.cpf': ('CPF do dependente', 'cpf'),
 'dependente.beneficiario': ('Dependente beneficiário BPC', 'choice'),
 'dependente.beneficio_numero': ('Número do benefício do dependente', 'text'),
 'formulario.exercicio': ('Exercício do IPTU', 'year'),
 'formulario.numero_processo': ('Número do processo no formulário', 'text'),
 'formulario.observacoes': ('Observações (revisão manual)', 'text'),
}

# Patterns identify labels on OCR rows, not arbitrary occurrences of values.
LABELS = [
 ('conjuge.nome', r'nome do conjuge(?: ou companheiro)?\s*[:.]?(?=\s|$)'),
 ('conjuge.rg', r'rg do conjuge(?: ou companheiro)?\s*[:.]?(?=\s|$)'),
 ('conjuge.cpf', r'(?:cin\s*/\s*)?cpf do conjuge(?: ou companheiro)?\s*[:.]?(?=\s|$)'),
 ('dependente.rg', r'rg do dependente\s*[:.]?(?=\s|$)'),
 ('dependente.cpf', r'(?:cin\s*/\s*)?cpf do dependente\s*[:.]?(?=\s|$)'),
 ('imovel.residente_edicula', r'nome de quem reside na edicula\s*[:.]?(?=\s|$)'),
 ('imovel.rg_residente_edicula', r'rg de quem reside na edicula\s*[:.]?(?=\s|$)'),
 ('formulario.numero_processo', r'n[º°o]?\s*processo\s*[:.]?(?=\s|$)'),
 ('requerente.nome', r'^\s*nome(?!\s+(?:do|de|da)\b)\s*:?(?=\s|$)'),
 ('requerente.rg', r'\brg(?!\s+(?:do|de|da)\b)\s*:?(?=\s|$)'),
 ('requerente.cpf', r'(?:cin\s*/\s*)?cpf\s*[:.]?(?=\s|$)'),
 ('requerente.data_nascimento', r'data\s+de\s+nascimento\s*[:.]?(?=\s|$)'),
 ('requerente.telefone', r'tel\s*/?\s*cel\s*[:.]?(?=\s|$)'),
 ('requerente.email', r'e\s*-?\s*mail\s*[:.]?(?=\s|$)'),
 ('endereco.logradouro', r'endereco\s*[:.]?(?=\s|$)'),
 ('endereco.numero', r'^\s*n[º°o]?\s*[:.]?(?=\s*[\[|]?\s*\d)'),
 ('endereco.complemento', r'complemento\s*[:.]?(?=\s|$)'),
 ('endereco.bairro', r'bairro\s*[:.]?(?=\s|$)'),
 ('endereco.cep', r'cep\s*[:.]?(?=\s|$)'),
 ('imovel.cadastro', r'n[º°o]?\s*cadastro\s+do\s+imovel\s*[:.]?(?=\s|$)'),
 ('imovel.tipo', r'tipo\s+do\s+imovel\s*[:.]?(?=\s|$)'),
 ('imovel.tipo_escritura', r'tipo\s+de\s+escritura\s*[:.]?(?=\s|$)'),
 ('imovel.aluguel_edicula', r'valor\s+do\s+aluguel\s*[:.]?(?=\s|$)'),
 ('dependente.parentesco', r'parentesco\s*[:.]?(?=\s|$)'),
 ('_beneficio', r'n[º°o]?\s*beneficio\s*[:.]?(?=\s|$)'),
 ('_orgao', r'orgao\s*[:.]?(?=\s|$)'),
 ('_valor', r'valor\s*[:.]?(?=\s|$)'),
 ('_data', r'data\s+da\s+concessao\s*[:.]?(?=\s|$)'),
]
CHOICES = [
 ('requerente.estado_civil', r'estado civil', ['solteiro','casado','uniao estavel','divorciado','viuvo']),
 ('requerente.primeira_isencao', r'primeira isencao', ['sim','nao']),
 ('requerente.pessoa_com_deficiencia', r'(?:pessoa com|pessoa de) deficiencia', ['sim','nao']),
 ('beneficios.recebe', r'recebe beneficio', ['sim','nao']),
 ('beneficios.aposentado', r'e aposentado', ['sim','nao']),
 ('beneficios.pensionista', r'e pensionista', ['sim','nao']),
 ('beneficios.tipo', r'tipo de beneficio', ['aposentadoria','renda mensal vitalicia','bpc','deficiente fisico','amparo social ao idoso','loas','nao se aplica']),
 ('beneficios.outras_rendas', r'possui outras fontes', ['sim','nao']),
 ('imovel.usufrutuario', r'caso de usufrutuario', ['sim','nao']),
 ('imovel.reside_no_imovel', r'beneficiario reside', ['sim','nao']),
 ('imovel.ponto_comercial', r'imovel possui ponto', ['sim','nao']),
 ('imovel.possui_edicula', r'imovel possui edicula', ['sim','nao']),
 ('imovel.edicula_habitada', r'edicula esta habitada', ['sim','nao','nao se aplica']),
 ('imovel.edicula_alugada', r'edicula esta alugada', ['sim','nao','nao se aplica']),
 ('imovel.possui_outro', r'possui outro imovel', ['sim','nao']),
 ('atividade.socio_empresa', r'e socio de empresa', ['sim','nao']),
 ('atividade.autonomo', r'trabalha como autonomo', ['sim','nao']),
 ('atividade.registrado', r'trabalha registrado', ['sim','nao']),
 ('conjuge.beneficiario', r'conjuge ou companheiro e', ['sim','nao','nao se aplica']),
 ('conjuge.socio_empresa', r'conjuge e socio', ['sim','nao','nao se aplica']),
 ('conjuge.outras_rendas', r'conjuge ou companheiro possui', ['sim','nao','nao se aplica']),
 ('conjuge.possui_outro_imovel', r'conjuge possui outro', ['sim','nao','nao se aplica']),
 ('dependente.possui_deficiencia', r'tem dependente portador', ['sim','nao']),
 ('dependente.beneficiario', r'dependente e beneficiario', ['sim','nao','nao se aplica']),
]


def fold(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.lower()) if not unicodedata.combining(c))


def cpf_valid(text):
    n = re.sub(r'\D', '', text)
    if len(n) != 11 or len(set(n)) == 1:
        return False
    for size in (9,10):
        digit = (sum(int(n[i]) * (size+1-i) for i in range(size)) * 10) % 11 % 10
        if digit != int(n[size]):
            return False
    return True


def convert(raw, kind):
    raw = raw.strip(' :|_\t[]{}')
    if not raw:
        return None, 'vazio'
    if fold(raw) in ('nao possui','nao se aplica'):
        return None, 'nao_aplicavel_declarado'
    if kind == 'cpf':
        digits = re.sub(r'\D', '', raw)
        return (digits, None) if cpf_valid(raw) else (None, 'cpf_invalido')
    if kind in ('cep','phone'):
        digits = re.sub(r'\D', '', raw)
        valid = len(digits)==8 if kind=='cep' else len(digits) in (10,11)
        return (digits, None) if valid else (None, 'formato_invalido')
    if kind == 'date':
        for fmt in ('%d/%m/%Y','%d-%m-%Y','%d.%m.%Y'):
            try:
                return datetime.strptime(raw.replace(' ', ''), fmt).date().isoformat(), None
            except ValueError:
                pass
        return None, 'data_invalida'
    if kind == 'money':
        match = re.fullmatch(r'(?:R\$\s*)?([\d.]+),\s*(\d{2})', raw, re.I)
        if match:
            return match.group(1).replace('.', '')+'.'+match.group(2), None
        return None, 'valor_monetario_invalido'
    if kind == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',raw):
        return None, 'email_invalido'
    if kind == 'year' and not re.fullmatch(r'20\d{2}',raw):
        return None,'exercicio_invalido'
    return raw, None


def rows_from_words(words):
    rows=[]
    for word in sorted(words,key=lambda w:(w['y']+w['h']/2,w['x'])):
        center=word['y']+word['h']/2
        row=next((r for r in reversed(rows[-5:]) if abs(r['center']-center)<max(8,word['h']*.52)),None)
        if row is None:
            row={'center':center,'words':[]}; rows.append(row)
        row['words'].append(word)
    for row in rows:
        row['words'].sort(key=lambda w:w['x'])
        row['text']=' '.join(w['text'] for w in row['words'])
        row['y']=min(w['y'] for w in row['words'])
        row['h']=max(w['y']+w['h'] for w in row['words'])-row['y']
    return sorted(rows,key=lambda r:r['y'])


def checkbox_candidates(image):
    gray=np.array(image.convert('L'))
    _,binary=cv2.threshold(gray,170,255,cv2.THRESH_BINARY_INV)
    contours,_=cv2.findContours(binary,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    boxes=[]
    scale=image.width/2480
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if not (12*scale<=w<=48*scale and 12*scale<=h<=48*scale and .72<w/h<1.35):
            continue
        perimeter=cv2.arcLength(contour,True)
        if len(cv2.approxPolyDP(contour,.035*perimeter,True))!=4:
            continue
        if any(abs(x-b['x'])<5*scale and abs(y-b['y'])<5*scale for b in boxes):
            continue
        margin=max(3,round(min(w,h)*.22))
        inside=binary[y+margin:y+h-margin,x+margin:x+w-margin]
        if not inside.size:
            continue
        density=float(np.count_nonzero(inside)/inside.size)
        # Middle band is uncertain. Empty boxes never imply a negative answer.
        state='marcada' if density>.17 else 'vazia' if density<.045 else 'incerta'
        boxes.append(dict(x=x,y=y,w=w,h=h,density=round(density,3),state=state))
    return boxes


def ocr_page(page,directory,index,dpi):
    pix=page.get_pixmap(dpi=dpi,colorspace=fitz.csRGB)
    image=Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
    path=directory/f'full-{index}.png';image.save(path,dpi=(dpi,dpi))
    prefix=directory/f'ocr-{index}'
    run=subprocess.run(['tesseract',str(path),str(prefix),'-l','por','--psm','11','txt','tsv','pdf'],
                       capture_output=True,text=True,timeout=180)
    if run.returncode:
        raise RuntimeError('Falha no OCR completo da página.')
    words=[]
    for row in csv.DictReader(io.StringIO(prefix.with_suffix('.tsv').read_text()),delimiter='\t'):
        if row['text'].strip() and float(row['conf'])>=0:
            words.append(dict(text=row['text'],confidence=round(float(row['conf'])/100,3),
                              **{key:int(row[source]) for key,source in [('x','left'),('y','top'),('w','width'),('h','height')]}))
    return dict(text=prefix.with_suffix('.txt').read_text(),words=words,width=image.width,height=image.height,
                boxes=checkbox_candidates(image),pdf=prefix.with_suffix('.pdf'))


def empty_document(process,manifest):
    return dict(schema_version='2.0',processo=process,gerado_em=datetime.now(timezone.utc).isoformat(),
        motor='tesseract-por-local',revisao_humana=False,status='revisao_necessaria',
        avisos=['Dados preliminares. Não utilizar para decidir concessão de isenção sem conferência.',
                'Confiança do OCR não é probabilidade de o campo estar correto.'],
        origem_preprocessamento=manifest.get('status'),campos={key:dict(rotulo=label,tipo=kind,valor=None,
            status='nao_identificado',confianca_ocr=None,evidencias=[]) for key,(label,kind) in FIELDS.items()},
        dados={},paginas=[],resumo={})


def add_value(document,key,raw,evidence):
    field=document['campos'][key]
    value,error=convert(raw,field['tipo'])
    evidence={**evidence,'texto':raw,'valor_candidato':value,'validacao':error or 'formato_valido'}
    field['evidencias'].append(evidence)
    if error is None and evidence.get('confianca_ocr') is not None and evidence['confianca_ocr'] < .60:
        if field['valor'] is None: field['status']='baixa_confianca'
        return
    if error:
        if field['valor'] is None:
            field['status']=error
        return
    if field['valor'] is not None and field['valor']!=value:
        field['valor']=None;field['confianca_ocr']=None;field['status']='conflito';return
    if field['status']=='conflito':
        return
    field['valor']=value;field['status']='extraido_nao_revisado'
    field['confianca_ocr']=evidence.get('confianca_ocr')


def parse_page(document,ocr,page_number,origin):
    rows=rows_from_words(ocr['words'])
    section='aposentadoria'
    context='requerente'
    pension_rows=[r for r in rows if re.search(r'^(?:e )?pensionista',fold(r['text']).strip())]
    pension_y=pension_rows[0]['y'] if pension_rows else None
    for i,row in enumerate(rows):
        text=row['text'].replace('|',' ').replace('[',' ').replace(']',' '); t=fold(text)
        if 'conjuge' in t:
            context='conjuge'
        elif 'dependente' in t:
            context='dependente'
        if pension_y is not None and row['y']>=pension_y-ocr['width']*.01 and context=='requerente':
            section='pensao'
        confidence=round(sum(w['confidence'] for w in row['words'])/len(row['words']),3)
        evidence=dict(pagina_extraida=page_number,origem=origin,linha=text,confianca_ocr=confidence,
                      bbox=[min(w['x'] for w in row['words']),row['y'],max(w['x']+w['w'] for w in row['words']),row['y']+row['h']])
        title=re.search(r'iptu\s+(20\d{2})',t)
        if title:
            add_value(document,'formulario.exercicio',title.group(1),evidence)
        matches=[]
        for key,pattern in LABELS:
            for match in re.finditer(pattern,t):
                matches.append((match.start(),match.end(),key))
        # More specific labels win overlapping spans.
        matches.sort(key=lambda m:(m[0],-(m[1]-m[0])))
        chosen=[]
        for match in matches:
            if not any(match[0]<end and match[1]>start for start,end,_ in chosen):
                chosen.append(match)
        chosen.sort()
        for n,(start,end,key) in enumerate(chosen):
            raw=text[end:chosen[n+1][0] if n+1<len(chosen) else len(text)].strip(' :|')
            if not raw:
                continue
            # A label in another column must not absorb checkboxes or the next question.
            if re.search(r'\b(?:sim|nao|casado|solteiro)\b',fold(raw)) and field_kind(key)!='email':
                continue
            if key.startswith('_'):
                target={'_beneficio':'numero','_valor':'valor','_orgao':'orgao','_data':'data'}[key]
                if context=='conjuge':
                    key='conjuge.beneficio_'+target
                elif context=='dependente':
                    key='dependente.beneficio_'+target
                else:
                    key='beneficios.'+section+'_'+target
                if key not in FIELDS:
                    continue
            if key=='requerente.nome' and context=='dependente':
                key='dependente.nome'
            if context in ('conjuge','dependente') and key in ('requerente.rg','requerente.cpf'):
                key=context+key[len('requerente'):]
            normalized=fold(raw).strip(' :|_[]{}.-')
            if key.endswith('.nome') and (normalized in ('ou','de','do','da') or
                    re.search(r'conjuge|companheiro|dependente',normalized)):
                continue
            if (key.endswith('.rg') or key.endswith('_numero')) and not re.search(r'\d{4}',raw):
                continue
            add_value(document,key,raw,evidence)
    parse_choices(document,ocr,rows,page_number,origin)


def field_kind(key):
    return FIELDS.get(key,('', 'text'))[1]


def parse_choices(document,ocr,rows,page_number,origin):
    # Checkbox answers are suggestions only and always require human review.
    # Use local text around each checkbox and nearest question in same column.
    width=ocr['width']
    anchors=[]
    for i,row in enumerate(rows):
        for key,pattern,options in CHOICES:
            t=fold(row['text'])
            match=re.search(pattern,t)
            if match:
                # Disambiguate generic questions from spouse/dependent variants.
                if key.startswith(('beneficios.','imovel.','atividade.')) and ('conjuge' in t or 'dependente' in t):
                    continue
                words=row['words']; before=row['text'][:match.start()]
                offset=len(before.split())
                x=words[min(offset,len(words)-1)]['x']
                anchors.append(dict(key=key,options=options,x=x,y=row['y']))
    suggestions={}
    for box in ocr['boxes']:
        if box['state']!='marcada':
            continue
        nearby=[w for w in ocr['words'] if w['x']>=box['x']+box['w']-4 and
                w['x']<box['x']+box['w']+width*.24 and
                abs(w['y']+w['h']/2-(box['y']+box['h']/2))<max(box['h'],w['h'])*.7]
        nearby.sort(key=lambda w:w['x'])
        if not nearby or nearby[0]['x']-box['x']-box['w']>width*.025:
            continue
        text=' '.join(w['text'] for w in nearby[:5]);label=fold(text).strip(' :|')
        candidates=[a for a in anchors if -15<=box['y']-a['y']<=width*.10 and
                    a['x']<=box['x']+20 and box['x']-a['x']<width*.42]
        candidates.sort(key=lambda a:(abs(box['y']-a['y'])+abs(box['x']-a['x'])*.22))
        if not candidates:
            continue
        anchor=candidates[0]
        option=next((o for o in sorted(anchor['options'],key=len,reverse=True) if label.startswith(o)),None)
        if option:
            suggestions.setdefault(anchor['key'],[]).append(dict(valor=option,pagina_extraida=page_number,
                origem=origin,bbox=[box['x'],box['y'],box['x']+box['w'],box['y']+box['h']],
                densidade=box['density'],texto=text,metodo='checkbox_visual_experimental'))
    for key,values in suggestions.items():
        field=document['campos'][key]
        field.setdefault('sugestoes',[]).extend(values)
        field['status']='marcacao_requer_revisao'
        # Never promote a heuristic checkbox suggestion to a confirmed JSON value.


def summarize(document):
    nested={}
    for key,field in document['campos'].items():
        group,name=key.split('.',1)
        nested.setdefault(group,{})[name]=field['valor']
    document['dados']=nested
    document['resumo']=dict(total_campos=len(FIELDS),preenchidos=sum(f['valor'] is not None for f in document['campos'].values()),
        nao_identificados=sum(f['status']=='nao_identificado' for f in document['campos'].values()),
        com_sugestoes=sum(bool(f.get('sugestoes')) for f in document['campos'].values()),
        conflitos=sum(f['status']=='conflito' for f in document['campos'].values()))
    return document


def extract_form(pdf_path,manifest,output,dpi=300,cancelled=lambda:False,progress=lambda n,total:None,page_cache=None):
    result=empty_document(manifest['process'],manifest)
    result.update(resolucao_dpi=dpi,versao_extrator='labels-v2-pilot')
    ocr_pages=[]; texts=[]
    with tempfile.TemporaryDirectory(prefix='iptu-ocr-') as temp,fitz.open(pdf_path) as original:
        for i,page in enumerate(original):
            if cancelled():
                raise InterruptedError()
            progress(i+1,len(original))
            origin=manifest.get('selected',[])[i] if i<len(manifest.get('selected',[])) else {}
            ocr=page_cache.get_page_ocr(origin,dpi) if page_cache else None
            if ocr is None:
                ocr=ocr_page(page,Path(temp),i,dpi)
                if page_cache:page_cache.store_page_ocr(origin,dpi,ocr)
            parse_page(result,ocr,i+1,origin)
            result['paginas'].append(dict(pagina=i+1,origem=origin,largura=ocr['width'],altura=ocr['height'],
                palavras=len(ocr['words']),caixas_detectadas=len(ocr['boxes'])))
            texts.append(ocr['text']);ocr_pages.append(ocr['pdf'])
        if cancelled():
            raise InterruptedError()
        # Image-backed searchable PDF, separate from the extracted original-page PDF.
        temp_pdf=output/'formulario_ocr.tmp.pdf'
        with fitz.open() as combined:
            for path in ocr_pages:
                with fitz.open(path) as single:
                    combined.insert_pdf(single)
            combined.save(temp_pdf)
        temp_pdf.replace(output/'formulario_ocr.pdf')
    summarize(result)
    text_tmp=output/'ocr.tmp.txt';text_tmp.write_text('\n\f\n'.join(texts));text_tmp.replace(output/'ocr.txt')
    data_tmp=output/'dados.tmp.json';data_tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2));data_tmp.replace(output/'dados.json')
    return result


def apply_review(document,changes,reviewer):
    if not isinstance(changes,dict) or not changes or set(changes)-set(FIELDS):
        raise ValueError('Campos de revisão inválidos.')
    edits=[]
    for key,value in changes.items():
        if value is not None and not isinstance(value,str):
            raise ValueError('Valores devem ser texto ou null.')
        if value is not None and len(value)>2000:
            raise ValueError('Valor excede 2000 caracteres.')
        field=document['campos'][key]
        # Review inputs use the normalized schema formats for date/money.
        kind=field['tipo']
        if value is not None and value.strip():
            if kind=='date':
                try: datetime.strptime(value,'%Y-%m-%d')
                except ValueError: raise ValueError('Use data no formato AAAA-MM-DD.')
            elif kind=='money':
                if not re.fullmatch(r'\d+\.\d{2}',value): raise ValueError('Use valor monetário como 1621.00.')
            elif kind!='choice':
                normalized,error=convert(value,kind)
                if error: raise ValueError(f"{field['rotulo']}: {error}.")
                value=normalized
        else:
            value=None
        edits.append(dict(campo=key,antes=field['valor'],depois=value))
        field.update(valor=value,status='revisado_manualmente')
    document.setdefault('historico_revisao',[]).append(dict(em=datetime.now(timezone.utc).isoformat(),
                                                         usuario=reviewer,alteracoes=edits))
    document['revisao_humana']=True
    document['status']='revisao_parcial'
    if all(f['status']=='revisado_manualmente' for f in document['campos'].values()):
        document['status']='revisado'
    return summarize(document)
