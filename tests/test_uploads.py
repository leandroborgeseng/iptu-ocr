import pytest
from app.uploads import group_items,relative_pdf_path,LocalUploadSource


def test_auto_grouping_keeps_nested_files_in_the_same_process():
    paths=['Raiz/P1/a.pdf','Raiz/P1/anexos/profundos/b.pdf','Raiz/P2/sub/c.pdf']
    groups=group_items([dict(relative=p) for p in paths])
    assert list(groups)==['P1','P2'] and len(groups['P1'])==2


def test_single_process_selection_preserves_root_process_name():
    groups=group_items([dict(relative='3516200.410.00010907-2026-51/anexos/a.pdf')])
    assert list(groups)==['3516200.410.00010907-2026-51']
    assert list(group_items([dict(relative='Nome/sub/a.pdf')],'single'))==['Nome']
    assert list(group_items([dict(relative='Nome/a.pdf')]))==['Nome']
    assert list(group_items([dict(relative='a.pdf')]))==['a']


def test_unicode_names_are_preserved_and_source_paths_cannot_escape(tmp_path):
    assert relative_pdf_path('Isenção/Processo 01/anexos/Cópia.PDF')=='Isenção/Processo 01/anexos/Cópia.PDF'
    with pytest.raises(ValueError):LocalUploadSource(tmp_path,'../escape')
    source=LocalUploadSource(tmp_path,'a'*32)
    with pytest.raises(ValueError):source.copy(dict(path='../escape.pdf'),tmp_path/'out.pdf')


def test_multiple_selected_process_folders_keep_their_nested_files():
    files=[dict(relative=p) for p in ['P1/a.pdf','P1/anexos/b.pdf','P2/c.pdf','P2/sub/deep/d.pdf']]
    groups=group_items(files)
    assert set(groups)=={'P1','P2'}
    assert len(groups['P1'])==2 and len(groups['P2'])==2
    assert groups['P1'][1]['process_relative']=='anexos/b.pdf'
