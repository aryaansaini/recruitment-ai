
import pathlib
import sys

PATH = pathlib.Path('templates/index.html')
if not PATH.exists():
    sys.exit('templates/index.html not found. Run this from the folder that contains manage.py.')

html = PATH.read_text(encoding='utf-8')
if 'Evidence from the resume' in html:
    sys.exit('Already patched: the evidence block is already in index.html. Nothing to do.')

OLD = '<p style="margin:12px 0 0"><b>AI reason:</b> ${esc(d.reason)}</p>'

NEW = '''<p style="margin:12px 0 0"><b>AI reason:</b> ${esc(d.reason)}</p>
    ${d.breakdown ? `<div style="margin-top:12px">${[['Skills', d.breakdown.skills], ['Experience', d.breakdown.experience], ['Education', d.breakdown.education]].map(([l, v]) => `<div class="fun" style="grid-template-columns:90px 1fr 34px"><span>${l}</span><div class="bar"><i style="width:${Math.min(100, v)}%"></i></div><b>${esc(v)}</b></div>`).join('')}</div>` : ''}
    ${(d.evidence || []).length ? `<div style="margin-top:12px"><b>Evidence from the resume</b>${d.evidence.map(e => `<small style="display:block;color:var(--muted);margin-top:6px">“${esc(e.quote)}” <span class="chip">${esc(e.section)}</span> ${esc(e.supports)}</small>`).join('')}</div>` : ''}
    ${d.retrieval && d.retrieval !== 'semantic' ? `<small style="display:block;color:var(--muted);margin-top:10px">Scored without semantic search (${esc(d.retrieval)}).</small>` : ''}'''

if html.count(OLD) != 1:
    sys.exit(f'Could not patch: expected 1 match for the "AI reason" line, found {html.count(OLD)}. Nothing was changed. Send me this message.')

PATH.with_suffix('.html.bak').write_text(html, encoding='utf-8')
PATH.write_text(html.replace(OLD, NEW, 1), encoding='utf-8')
print('Done. Backup saved as templates/index.html.bak')
print('Now hard-refresh the browser (Ctrl + Shift + R), then run the pipeline on a candidate.')