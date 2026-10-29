/* Remember only non-secret connection fields. Passwords never enter browser storage. */
window.IPTUConnectionProfile = (() => {
  const key = 'iptu.connection.draft.v1';
  const fields = ['server','share','subpath','domain','username','stable_seconds'];
  function sanitize(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(fields.filter(k => typeof value[k] === 'string' && value[k].length <= 500)
      .map(k => [k,value[k]]));
  }
  function read() {
    try { return sanitize(JSON.parse(localStorage.getItem(key) || '{}')); } catch { return {}; }
  }
  function save(form) {
    const data = Object.fromEntries(fields.map(k => [k,form.elements[k].value]));
    try { localStorage.setItem(key,JSON.stringify(data)); } catch { /* Server-side save still works. */ }
  }
  function apply(form) {
    const draft = read();
    // A setup link carries no password and retains the username already saved in the app.
    const query = location.hash.includes('?') ? location.hash.slice(location.hash.indexOf('?')+1) : '';
    const params = new URLSearchParams(query);
    let linked = false;
    for (const name of ['server','share','subpath','domain']) {
      if (params.has(name)) { draft[name] = params.get(name).slice(0,500); linked = true; }
    }
    let changed = false;
    for (const [name,value] of Object.entries(draft)) {
      if (form.elements[name].value !== value) { form.elements[name].value = value; changed = true; }
    }
    if (linked) {
      save(form);
      // Remove connection metadata from the address after the profile is applied.
      history.replaceState(null,'',location.pathname+location.search+'#conexao');
    }
    return {changed,linked};
  }
  return {apply,save};
})();
