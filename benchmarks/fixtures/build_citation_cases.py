"""Controlled citation rule fixtures; not human evaluation of claim truth."""
from __future__ import annotations
import json
from pathlib import Path

DEST=Path(__file__).resolve().parents[1]/'datasets'/'citation_contract.v1.jsonl'
HEADING='## \u53c2\u8003\u6587\u732e'
BASE='The pilot documented an initial output rate.'


def make():
    rows=[]
    def append(kind, i, report, source, ok, semantic=None):
        rows.append({'id':f'citation-{len(rows)+1:03d}', 'category':kind,
              'report':report,'research_material':source,'expected_static_ok':ok,
              'known_semantic_support':semantic,'synthetic':True})
    for i in range(1,5):
        url=f'https://reference-{i}.example/paper'
        plain=f'# Summary\n\n{BASE} [{i}]'
        # Use a citation with matching integer, even if i >1 introduces a gap.
        valid=f'# Summary\n\n{BASE} [1]\n\n{HEADING}\n[1] Primary report {url}'
        append('valid_local_reference',i,valid,f'The report source is {url}',True,True)
        append('missing_source_section',i,plain,f'The report source is {url}',False,None)
        append('dangling_reference',i,f'# Summary\n\n{BASE} [2]\n\n{HEADING}\n[1] Primary report {url}',f'Observed {url}',False,None)
        bad=f'https://unknown-{i}.example/missing'
        append('url_not_in_material',i,f'# Summary\n\n{BASE} [1]\n\n{HEADING}\n[1] Bad source {bad}',f'Source {url}',False,None)
        append('duplicate_source_number',i,f'# Summary\n\n{BASE} [1]\n\n{HEADING}\n[1] Primary report {url}\n[1] Repeated {url}',f'Source {url}',False,None)
        append('unused_source',i,f'# Summary\n\n{BASE} [1]\n\n{HEADING}\n[1] Used {url}\n[2] Not referenced {url}',f'Source {url}',False,None)
        append('unsupported_semantics_not_detected',i,f'# Summary\n\nThe device performed perfectly in every trial. [1]\n\n{HEADING}\n[1] Evidence {url}',f'Original study at {url} only documented a calibration method, no performance conclusion.',True,False)
        append('missing_first_source_number',i,f'# Summary\n\n{BASE} [2]\n\n{HEADING}\n[2] Source {url}',f'Source {url}',False,None)
    assert len(rows)==32
    DEST.write_text(''.join(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n' for row in rows),encoding='utf-8')
    return len(rows)

if __name__=='__main__':
    print(make())
