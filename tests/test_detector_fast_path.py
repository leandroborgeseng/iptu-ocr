from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pymupdf as fitz
from app.detector import read_page,reliable_native_body

BODY=('Este documento digital possui o texto completo de um processo administrativo para conferencia e emissao de despacho.\n'*12)
FRONT='Prefeitura requerimento de isencao de IPTU 2027 nome imovel'


def test_complete_digital_nonform_avoids_rendering_and_tesseract(tmp_path):
    with fitz.open() as doc:
        page=doc.new_page();page.insert_textbox(fitz.Rect(35,130,550,750),BODY,fontsize=10)
        with patch('app.detector.subprocess.run',side_effect=AssertionError('OCR should not run')):
            _,decision,_=read_page(page,tmp_path,'digital')
        assert not decision['candidate'] and decision['evidence']['leitura']=='texto_digital_completo'


def test_sei_header_and_large_scan_still_use_ocr(tmp_path):
    with fitz.open() as scan:
        scanned=scan.new_page();scanned.insert_text((35,80),FRONT,fontsize=10)
        image=scanned.get_pixmap().tobytes('png')
    with fitz.open() as doc:
        page=doc.new_page();page.insert_textbox(fitz.Rect(35,15,550,90),('Cabecalho SEI administrativo de um processo para conferencia. '*4),fontsize=8)
        page.insert_image(fitz.Rect(35,110,550,800),stream=image)
        responses=[SimpleNamespace(stdout='',returncode=0),SimpleNamespace(stdout=FRONT,returncode=0)]
        with patch('app.detector.subprocess.run',side_effect=responses) as ocr:
            _,decision,_=read_page(page,tmp_path,'scan')
        assert ocr.call_count==2 and decision['roles']==['inicio']


def test_digital_text_alongside_image_does_not_bypass_ocr():
    with fitz.open() as scan:
        image=scan.new_page().get_pixmap().tobytes('png')
    with fitz.open() as doc:
        page=doc.new_page();page.insert_textbox(fitz.Rect(35,130,550,750),BODY,fontsize=10)
        page.insert_image(fitz.Rect(35,400,550,800),stream=image)
        assert not reliable_native_body(page,page.get_text())


def test_ambiguous_native_candidate_keeps_ocr_fallback(tmp_path):
    with fitz.open() as doc:
        page=doc.new_page();page.insert_textbox(fitz.Rect(35,130,550,750),'Isencao IPTU\n'+BODY,fontsize=10)
        with patch('app.detector.subprocess.run',side_effect=[SimpleNamespace(stdout='',returncode=0),SimpleNamespace(stdout=FRONT,returncode=0)]) as ocr:
            _,decision,_=read_page(page,tmp_path,'candidate')
        assert ocr.call_count==2 and 'inicio' in decision['roles']


def test_damaged_title_requires_year_and_strong_form_structure():
    from app.detector import classify
    text='Prefeitura titulo ilegivel ISENCAO DE IPTU 2027 nome imovel data de nascimento estado civil endereco renda cadastro CPF'
    assert 'inicio' in classify(text)['roles']
    assert 'inicio' not in classify('Prefeitura isencao de IPTU 2027 nome imovel')['roles']
    assert 'inicio' not in classify(text.replace('2027','2026'))['roles']


def test_garbled_digital_body_does_not_bypass_ocr():
    with fitz.open() as doc:
        page=doc.new_page();page.insert_textbox(fitz.Rect(35,130,550,750),('qwerty abcdef zxcvbn qazwsx asdfgh\n'*20),fontsize=10)
        assert not reliable_native_body(page,page.get_text())
