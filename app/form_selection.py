"""Select at most one unambiguous form for the requested tax exercise."""
from .detector import assemble

SELECTION_POLICY='single-form-2027-v2'


def years(page):return set(page.get('evidence',{}).get('exercicios',[]))


def select_form(pages,target=2027):
    fronts=[p for p in pages if 'inicio' in p['roles']]
    targets=[]
    for front in fronts:
        detected=years(front)
        if not detected:
            siblings=[p for p in pages if p['file_index']==front['file_index'] and p['roles'] and abs(p['page']-front['page'])<=2]
            if sum(p['file_index']==front['file_index'] for p in fronts)==1:
                detected=set().union(*(years(p) for p in siblings))
        if detected=={target}:targets.append(front)
    if len(targets)!=1:
        reason='Mais de um formulário de 2027; é necessária revisão para escolher um.' if len(targets)>1 else 'Nenhum formulário com exercício 2027 confirmado. Ano ausente ou outro exercício.'
        only_other_years=bool(fronts) and all(years(p) and target not in years(p) for p in fronts)
        return ('nao_encontrado' if only_other_years else 'revisao' if any(p['candidate'] for p in pages) else 'nao_encontrado'),[],reason
    front=targets[0];chosen=[front]
    for page in pages:
        if page is front or not page['roles'] or 'inicio' in page['roles']:continue
        if years(page) and years(page)!={target}:continue
        same_file=[p for p in fronts if p['file_index']==page['file_index'] and (not years(page) or not years(p) or years(p)==years(page))]
        belongs=False
        if same_file:
            distance=min(abs(p['page']-page['page']) for p in same_file)
            nearest=[p for p in same_file if abs(p['page']-page['page'])==distance]
            belongs=len(nearest)==1 and nearest[0] is front
        elif len(fronts)==1:
            belongs=True
        if belongs:chosen.append(page)
    # Multiple continuation/end pages may represent multiple forms: never concatenate them.
    if any(sum(role in p['roles'] for p in chosen)>1 for role in ('continuacao','encerramento')):
        return 'revisao',[],'Páginas duplicadas ou associação ambígua: nenhum formulário único foi escolhido.'
    status,ordered=assemble(chosen)
    if len(chosen)==1 and all(role in front['roles'] for role in ('inicio','continuacao','encerramento')):status='completo_provavel'
    if any(p['candidate'] and not p['roles'] and p['file_index']==front['file_index'] and (not years(p) or years(p)=={target}) for p in pages):status='revisao'
    return status,ordered,'Exercício 2027 confirmado; um formulário, ordenado pelo conteúdo das páginas.'
