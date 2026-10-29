"""Run from the project folder:  python patch_otp_ui.py
Edits templates/index.html. If ANY anchor is not found it prints what is missing and changes nothing."""
import pathlib
import re

JS_AUTH = r"""let authMode = 'login', pendingUser = '', resendTimer = null;
function setAuthMode(m) {
  authMode = m;
  $('cred_fields').style.display = m === 'otp' ? 'none' : '';
  $('signup_extra').style.display = m === 'signup' ? 'block' : 'none';
  $('otp_box').style.display = m === 'otp' ? 'block' : 'none';
  $('login_title').textContent = m === 'login' ? 'Sign in to Recruit AI' : m === 'signup' ? 'Create your account' : 'Verify your email';
  $('login_btn').textContent = m === 'login' ? 'Sign in' : m === 'signup' ? 'Send verification code' : 'Verify and continue';
  $('login_pass').autocomplete = m === 'login' ? 'current-password' : 'new-password';
  $('login_switch').innerHTML = m === 'login'
    ? (window.signupsOpen === false ? '' : 'New here? <a href="#" onclick="setAuthMode(\'signup\');return false">Create an account</a>')
    : m === 'signup' ? 'Already have an account? <a href="#" onclick="setAuthMode(\'login\');return false">Sign in</a>'
    : '<a href="#" onclick="setAuthMode(\'signup\');return false">Use a different email</a>';
  $('login_error').textContent = '';
}
function startCooldown(sec) {
  clearInterval(resendTimer); const b = $('otp_resend'); let n = sec;
  const tick = () => { b.disabled = n > 0; b.textContent = n > 0 ? `Resend code (${n}s)` : 'Resend code'; n--; if (n < 0) clearInterval(resendTimer); };
  tick(); resendTimer = setInterval(tick, 1000);
}
function saveSession(d, username) {
  localStorage.setItem('access', d.access); localStorage.setItem('refresh', d.refresh); localStorage.setItem('username', username);
  $('login_pass').value = ''; $('otp_code').value = ''; $('login_overlay').style.display = 'none'; start();
}
async function login() {
  if (authMode === 'otp') return verifyOtp();
  const username = $('login_user').value.trim(), password = $('login_pass').value;
  if (authMode === 'signup') {
    const res = await fetch('/api/auth/signup/', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username, password, email: $('su_email').value.trim(), invite_code: $('su_invite').value.trim()})});
    const data = await readJson(res);
    if (!res.ok) { $('login_error').textContent = res.status === 429 ? 'Too many attempts. Wait a while.' : errText(data); return; }
    pendingUser = username; setAuthMode('otp');
    $('otp_info').textContent = 'We sent a 6-digit code to ' + data.email + '. It expires in 10 minutes. Please also check your spam folder.';
    startCooldown(60); $('otp_code').focus(); return;
  }
  const res = await fetch('/api/auth/login/', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username, password})});
  const data = await readJson(res);
  if (!res.ok) {
    $('login_error').textContent = res.status === 429 ? 'Too many attempts. Wait a while.'
      : res.status === 401 ? 'Wrong username or password (or your email is not verified yet).' : errText(data);
    return;
  }
  saveSession(data, username);
}
async function verifyOtp() {
  const res = await fetch('/api/auth/verify-otp/', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: pendingUser, code: $('otp_code').value.trim()})});
  const data = await readJson(res);
  if (!res.ok) { $('login_error').textContent = res.status === 429 ? 'Too many attempts. Wait a while.' : errText(data); return; }
  saveSession(data, pendingUser);
}
async function resendOtp() {
  const res = await fetch('/api/auth/resend-otp/', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: pendingUser})});
  const data = await readJson(res);
  if (!res.ok) { $('login_error').textContent = errText(data); return; }
  $('login_error').textContent = ''; toast('A new code was sent.'); startCooldown(60);
}
"""

JS_PROFILE = r"""// profile
let me = null;
async function loadProfile() { const r = await api('/api/auth/profile/'); if (r.ok) { me = await readJson(r); renderProfile(); } }
function renderProfile() {
  if (!$('pf_box')) return;
  const u = (me && me.username) || localStorage.getItem('username') || '', demo = isDemo();
  $('pf_box').innerHTML = `<span class="av" style="width:56px;height:56px;font-size:20px">${demo ? 'D' : ini(u)}</span><div class="t"><b style="font-size:18px">${esc(demo ? 'Demo visitor' : u)}</b>${demo ? '<small>Temporary workspace</small>' : `<small>${esc((me && me.email) || '')} <span class="lbl" style="color:var(--ok);border-color:var(--ok)">verified</span></small><small>Member since ${me && me.date_joined ? when(me.date_joined).slice(0, 10) : '-'}</small>`}</div>`;
  $('pf_data').innerHTML = demo
    ? `<p class="hint" style="margin-top:0">This is a temporary demo workspace. Everything you add here is deleted automatically when you sign out or close this page. To keep your data, create a free account.</p><button class="btn s" onclick="leaveDemo()">Create account</button>`
    : `<p class="hint" style="margin-top:0">Everything you add (jobs, candidates, resumes) is private to your account. You can delete it at any time.</p><div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn s sec" onclick="clearData()">Delete all my jobs and candidates</button><button class="btn s sec" onclick="deleteAccount()">Delete my account and data</button></div>`;
}
async function clearData() {
  if (!confirm('Delete ALL your jobs, candidates and resumes? Your account stays.')) return;
  const r = await api('/api/auth/clear-data/', {method: 'POST'});
  if (!r.ok) return toast(errText(await readJson(r)), 'err');
  await refresh(); loadLogs(); toast('All your jobs and candidates were deleted');
}
// a demo workspace is removed as soon as the page is closed (server also removes old ones after 2 hours)
window.addEventListener('pagehide', () => {
  if (!isDemo()) return;
  const t = localStorage.getItem('access');
  if (t) fetch('/api/auth/demo/end/', {method: 'POST', keepalive: true, headers: {Authorization: 'Bearer ' + t}});
});

"""

SIGNUP_HTML = """<div id="signup_extra" style="display:none">
    <label class="f">Email</label><input id="su_email" type="email" inputmode="email" autocomplete="email" placeholder="you@example.com">
    <div id="su_invite_wrap" style="display:none"><label class="f">Invite code</label><input id="su_invite" autocomplete="off"></div>
    <div class="hint">We will email you a 6-digit code to verify this address. Everything you add stays private to your account and you can delete it at any time from My Profile.</div>
  </div>
  <div id="otp_box" style="display:none">
    <div class="hint" id="otp_info" style="margin:0 0 8px"></div>
    <label class="f">6-digit code</label><input id="otp_code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="123456">
    <button class="btn sec full" id="otp_resend" style="margin-top:10px" onclick="resendOtp()">Resend code</button>
  </div>"""

PROFILE_SECTION = """<section class="view" id="v-profile">
    <div class="grid stats" id="pf_stats"></div>
    <div class="grid even">
      <div class="box"><h2>Account</h2><div class="bd"><div class="rw" id="pf_box" style="padding:0 0 14px"></div><button class="btn s sec" onclick="logout()">Sign out</button></div></div>
      <div class="box"><h2>Your data</h2><div class="bd" id="pf_data"></div></div>
    </div>
  </section>

  """


def main():
    path = pathlib.Path('templates/index.html')
    s = path.read_text(encoding='utf-8')
    problems = []

    def rx(name, pattern, repl, flags=re.S):
        nonlocal s
        found = re.findall(pattern, s, flags)
        if len(found) != 1:
            problems.append(f'{name}: found {len(found)} (need exactly 1)')
            return
        s = re.sub(pattern, lambda m: repl(m) if callable(repl) else repl, s, count=1, flags=flags)

    def lit(name, old, new):
        nonlocal s
        if s.count(old) != 1:
            problems.append(f'{name}: found {s.count(old)} (need exactly 1)')
            return
        s = s.replace(old, new)

    # --- login box ---
    rx('credential fields',
       r'(<label class="f">Username</label>\s*<input id="login_user"[^>]*>\s*<label class="f">Password</label>\s*<input id="login_pass"[^>]*>)',
       lambda m: '<div id="cred_fields">' + m.group(1) + '</div>')
    rx('signup block', r'<div id="signup_extra" style="display:none">.*?(?=\s*<div id="login_error")', SIGNUP_HTML)
    rx('demo hint', r'<div class="hint" style="text-align:center">The demo is a shared.*?</div>',
       '<div class="hint" style="text-align:center">The demo gives you full access with sample data. Anything you add is deleted automatically when you sign out or close the page. To keep your data, create a free account.</div>')
    rx('demo banner', r'<span><b>You are in the shared demo.*?</span>',
       '<span><b>You are in a temporary demo.</b> Everything you add is deleted when you sign out or close this page. Create a free account to keep your data.</span>')

    # --- tabs / views ---
    rx('dashboard tab', r'(<button class="tab" data-t="overview">.*?</button>)',
       lambda m: m.group(1) + '\n    <button class="tab" data-t="profile"><span class="i">☺</span>My Profile</button>')
    lit('profile section', '<section class="view" id="v-jobs">', PROFILE_SECTION + '<section class="view" id="v-jobs">')
    rx('old Your data box', r'<div class="box" id="my_data_box".*?Delete my account and data</button></div></div>', '')
    lit('titles', "rankings: ['Leaderboard', 'Top candidates by AI score']};",
        "rankings: ['Leaderboard', 'Top candidates by AI score'], profile: ['My Profile', 'Your account and your data']};")

    # --- script ---
    rx('auth js', r"let authMode = 'login';.*?(?=fetch\('/api/auth/config/'\))", JS_AUTH)
    rx('logout', r"function logout\(\) \{[^\n]*\}\n",
       "async function logout() { if (isDemo()) { try { await api('/api/auth/demo/end/', {method: 'POST'}, false); } catch (e) {} } "
       "['access', 'refresh', 'username'].forEach(k => localStorage.removeItem(k)); $('who').textContent = ''; showLogin('You have been signed out.'); }\n")
    lit('isDemo', "const isDemo = () => localStorage.getItem('username') === 'demo_recruiter';",
        "const isDemo = () => (localStorage.getItem('username') || '').startsWith('demo_');")
    lit('leaveDemo', "function leaveDemo() { logout(); setAuthMode('signup'); $('login_error').textContent = ''; }",
        "async function leaveDemo() { await logout(); setAuthMode('signup'); $('login_error').textContent = ''; }")
    lit('start()', "$('my_data_box').style.display = isDemo() ? 'none' : '';", 'loadProfile();')
    lit('stats mirror', "$('stats').innerHTML = cards.map(", "$('pf_stats').innerHTML = $('stats').innerHTML = cards.map(")
    lit('renderAll', "renderInterviews(); $('n_jobs')", "renderInterviews(); renderProfile(); $('n_jobs')")
    lit('profile js', '// start\nfunction start() {', JS_PROFILE + '// start\nfunction start() {')

    if problems:
        print('NOTHING CHANGED. These anchors were not found as expected:')
        for p in problems:
            print('  -', p)
        return
    path.with_suffix('.html.bak').write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
    path.write_text(s, encoding='utf-8')
    print('Done. Backup saved as templates/index.html.bak')


if __name__ == '__main__':
    main()