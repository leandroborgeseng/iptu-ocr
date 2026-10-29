from app.detector import classify
from app.form_selection import select_form

FRONT='Prefeitura requerimento de isencao de IPTU {} nome imovel'
BACK='Prefeitura conjuge dependente trabalha socio de empresa imovel assinatura requerente presente requerimento IPTU'

def page(text,n=1,file=0):return dict(page=n,file_index=file,**classify(text))


def test_inverted_two_page_form_is_exported_front_first():
    back=page(BACK,1);front=page(FRONT.format(2027),2)
    status,selected,_=select_form([back,front])
    assert status=='completo_provavel' and selected==[front,back]


def test_other_year_and_unconfirmed_year_do_not_export():
    for year in (2026,''):
        status,selected,_=select_form([page(FRONT.format(year)),page(BACK,2)])
        assert status==('nao_encontrado' if year==2026 else 'revisao') and selected==[]


def test_multiple_2027_forms_are_never_combined_or_arbitrarily_chosen():
    status,selected,_=select_form([page(FRONT.format(2027)),page(BACK,2),page(FRONT.format(2027),3),page(BACK,4)])
    assert status=='revisao' and selected==[]


def test_inverted_forms_with_explicit_years_can_be_separated():
    old_back=page(BACK+' exercicio 2026',1);old_front=page(FRONT.format(2026),2)
    back=page(BACK+' exercicio 2027',3);front=page(FRONT.format(2027),4)
    status,selected,_=select_form([old_back,old_front,back,front])
    assert status=='completo_provavel' and selected==[front,back]


def test_form_split_between_two_pdfs_without_competing_front():
    front=page(FRONT.format(2027),1,0);back=page(BACK,1,1)
    assert select_form([back,front])[1]==[front,back]


def test_unrelated_dates_do_not_confirm_exercise():
    result=classify(FRONT.format('')+' nascimento 01/01/2027 lei complementar 2027')
    assert result['evidence']['exercicios']==[]


def test_duplicate_continuations_require_review_without_extra_export():
    assert select_form([page(FRONT.format(2027)),page(BACK,2),page(BACK,3)])[1]==[]
