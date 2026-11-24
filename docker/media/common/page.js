// Shared by the status pages of media-ingest and media-subtitles: small helpers, the top bar linking the two
// pages with what waits for the user on each, and the live log.

// The pages under media-web (docker/media/media-web/nginx.conf); summary: where the count of what waits comes from
const SERVICES = [
  { name: "Подреждане", url: "/ingest/", summary: "/ingest/api/summary" },
  { name: "Актьори", url: "/ingest/people" },
  { name: "Субтитри и аудио", url: "/subtitles/", summary: "/subtitles/api/summary" },
];

const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) node.append(child);
  return node;
};
const swatch = key => {
  const s = el("span", { className: "swatch" });
  s.style.background = `var(--${key})`;
  return s;
};
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString("bg-BG", { dateStyle: "short", timeStyle: "short" }) : "—";

async function renderBar() {
  const links = await Promise.all(SERVICES.map(async s => {
    const here = location.pathname === s.url;
    let waiting = 0;
    if (s.summary) {
      try {
        waiting = (await (await fetch(s.summary)).json()).waiting;
      } catch (e) { waiting = null; }
    }
    const link = el(here ? "span" : "a", { className: here ? "here" : "", textContent: s.name });
    if (!here) link.href = s.url;
    if (waiting === null) link.append(" ", el("span", { className: "count off", textContent: "?", title: "не отговаря" }));
    else if (waiting) link.append(" ", el("span", { className: "count", textContent: waiting, title: "чакат теб" }));
    return link;
  }));
  document.getElementById("bar").replaceChildren(...links);
}

// kinds: the first word of a line -> [label, color]; now(res) shows what the service is doing and returns whether
// it is busy (polled every second then, otherwise every 5); onLines() runs when new lines came in.
// Returns poll, to look again right away.
function liveLog({ kinds, now, onLines }) {
  const log = document.getElementById("log");
  let seq = 0, boot = null, timer = null;

  function line(a) {
    const [word, ...rest] = a.text.split(" ");
    const [label, color] = kinds[word] || [word, null];
    const kind = el("span", { className: "kind", textContent: label });
    if (color) kind.style.color = `var(--${color})`;
    return el("li", {}, el("span", { className: "time", textContent: new Date(a.at * 1000).toLocaleTimeString("bg-BG") + " " }), kind, " " + rest.join(" "));
  }

  async function poll() {
    let busy = false;
    try {
      let res = await (await fetch(`api/activity?after=${seq}`)).json();
      // After a restart of the service its lines are numbered from 1 again, so the log starts over
      if (boot !== res.boot) {
        if (boot !== null) {
          seq = 0;
          log.replaceChildren();
          res = await (await fetch("api/activity?after=0")).json();
        }
        boot = res.boot;
      }
      busy = now(res);
      if (res.lines.length) {
        const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
        log.querySelector(".empty")?.remove();
        log.append(...res.lines.map(line));
        while (log.children.length > 300) log.firstChild.remove();
        if (atBottom) log.scrollTop = log.scrollHeight;
        seq = res.lines[res.lines.length - 1].seq;
        onLines();
      }
    } catch (e) { }
    clearTimeout(timer);
    timer = setTimeout(poll, busy ? 1000 : 5000);
  }

  poll();
  return poll;
}

renderBar();
setInterval(renderBar, 30000);
