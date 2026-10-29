from copy import deepcopy
import pytest
from PIL import Image,ImageDraw
from app.extraction import (empty_document,parse_page,summarize,convert,cpf_valid,add_value,
                            apply_review,checkbox_candidates)


def test_formats_and_no_guessed_digits():
    assert cpf_valid('111.444.777-35')
    assert not cpf_valid('111.444.777-34')
    assert not cpf_valid('11111111111')
    assert convert('111.444.777-34','cpf')==(None,'cpf_invalido')
    assert convert('31/02/2026','date')[0] is None
    assert convert('22/11/1955','date')[0]=='1955-11-22'
    assert convert('R$ 1.621,00','money')[0]=='1621.00'
    assert convert('1621','money')[0] is None
    assert convert('14400-000','cep')[0]=='14400000'
    assert convert('NAO POSSUI','email')==(None,'nao_aplicavel_declarado')


def test_duplicate_conflicting_values_remain_null():
    doc=empty_document('process',{})
    ev=dict(pagina_extraida=1,confianca_ocr=.99)
    for value in ('ANA TESTE','MARIA TESTE','ANA TESTE'):
        add_value(doc,'requerente.nome',value,ev)
    f=doc['campos']['requerente.nome']
    assert f['valor'] is None and f['status']=='conflito' and len(f['evidencias'])==3


def test_multi_column_values_and_provenance():
    lines=['REQUERIMENTO DE ISENCAO DE IPTU 2027','Nome: ANA TESTE',
           'RG: 123456 CIN/CPF: 111.444.777-35',
           'Data de Nascimento: 22/11/1955 Tel/Cel: 16999999999',
           'Endereco: RUA EXEMPLO','Nº: 100 Complemento: CASA',
           'Bairro: CENTRO CEP: 14400-000']
    words=[]
    for i,text in enumerate(lines):
        x=20
        for token in text.split():
            words.append(dict(text=token,x=x,y=30+i*40,w=len(token)*10,h=20,confidence=.97));x+=len(token)*10+10
    ocr=dict(words=words,boxes=[],width=2480,height=3500)
    doc=empty_document('p',{})
    parse_page(doc,ocr,1,dict(source='original.pdf',page=14))
    summarize(doc)
    assert doc['dados']['requerente']['nome']=='ANA TESTE'
    assert doc['dados']['requerente']['cpf']=='11144477735'
    assert doc['dados']['requerente']['data_nascimento']=='1955-11-22'
    assert doc['dados']['endereco']['numero']=='100'
    assert doc['dados']['endereco']['complemento']=='CASA'
    assert doc['campos']['requerente.nome']['evidencias'][0]['origem']['page']==14


def test_review_preserves_evidence_and_tracks_author():
    doc=empty_document('p',{})
    original=deepcopy(doc)
    apply_review(doc,{'requerente.nome':'CONFERIDO','imovel.possui_edicula':'nao','requerente.email':None},'admin')
    assert doc['dados']['requerente']['nome']=='CONFERIDO'
    assert doc['dados']['imovel']['possui_edicula']=='nao'
    assert doc['status']=='revisao_parcial'
    assert doc['historico_revisao'][0]['usuario']=='admin'
    assert original['campos']['requerente.nome']['valor'] is None
    for changes in ({}, {'unknown.field':'x'},{'requerente.cpf':'123'}, {'beneficios.pensao_valor':'1,00'}):
        with pytest.raises(ValueError):apply_review(deepcopy(original),changes,'admin')


def test_empty_checkbox_does_not_become_negative_answer():
    image=Image.new('RGB',(2480,500),'white');d=ImageDraw.Draw(image)
    d.rectangle((100,100,130,130),outline='black',width=3)
    d.rectangle((200,100,230,130),outline='black',width=3)
    d.line((204,104,226,126),fill='black',width=4);d.line((226,104,204,126),fill='black',width=4)
    boxes=checkbox_candidates(image)
    assert any(b['state']=='vazia' for b in boxes)
    assert any(b['state']=='marcada' for b in boxes)

def test_low_confidence_stays_null_and_wrapped_labels_are_not_names():
    doc=empty_document('p',{})
    add_value(doc,'requerente.nome','LEITURA INCERTA',dict(confianca_ocr=.2,pagina_extraida=1))
    assert doc['campos']['requerente.nome']['valor'] is None
    assert doc['campos']['requerente.nome']['status']=='baixa_confianca'


def test_address_number_does_not_absorb_benefit_number():
    lines=['Nº: 100 Complemento: CASA','É Aposentado? Sim Nº Benefício: 123456789 Valor:| 1621,00']
    words=[]
    for i,line in enumerate(lines):
        x=20
        for token in line.split():
            words.append(dict(text=token,x=x,y=50+i*50,w=len(token)*10,h=20,confidence=.99));x+=len(token)*10+10
    doc=empty_document('p',{})
    parse_page(doc,dict(words=words,boxes=[],width=2480,height=3500),1,{})
    assert doc['campos']['endereco.numero']['valor']=='100'
    assert doc['campos']['beneficios.aposentadoria_numero']['valor']=='123456789'
    assert doc['campos']['beneficios.aposentadoria_valor']['valor']=='1621.00'
