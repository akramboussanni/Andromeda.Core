"""Public landing page for an Andromeda Core deployment."""

LANDING_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#071018">
  <title>Andromeda — Enemy On Board, online again</title>
  <style>
    :root { --ink:#071018; --panel:#0d1b25; --line:#1d3442; --text:#f3f8f7;
      --muted:#94aaa9; --mint:#77f2cf; --sun:#ffc76b; --max:1180px; }
    * { box-sizing:border-box; }
    html { scroll-behavior:smooth; }
    body { margin:0; color:var(--text); background:var(--ink); font-family:Inter,ui-sans-serif,system-ui,sans-serif; }
    body:before { content:""; position:fixed; inset:0; pointer-events:none; opacity:.42;
      background:radial-gradient(circle at 82% 8%,#174e58 0,transparent 32rem),
      radial-gradient(circle at 10% 56%,#3d2e19 0,transparent 30rem); }
    a { color:inherit; }
    .wrap { width:min(calc(100% - 40px),var(--max)); margin:auto; position:relative; }
    header { height:76px; display:flex; align-items:center; justify-content:space-between; }
    .brand { display:flex; align-items:center; gap:11px; font-weight:800; letter-spacing:-.03em; text-decoration:none; }
    .mark { width:30px; height:30px; border:1px solid var(--mint); border-radius:50%; position:relative; }
    .mark:after { content:""; position:absolute; width:8px; height:8px; border-radius:50%; background:var(--sun); top:3px; right:-2px; }
    nav { display:flex; align-items:center; gap:24px; color:var(--muted); font-size:14px; }
    nav a { text-decoration:none; } nav a:hover { color:var(--text); }
    .button { display:inline-flex; align-items:center; justify-content:center; gap:8px; padding:12px 17px;
      border-radius:999px; border:1px solid var(--line); text-decoration:none; font-weight:700; font-size:14px; }
    .button.primary { color:var(--ink); background:var(--mint); border-color:var(--mint); }
    .button:hover { transform:translateY(-1px); }
    .hero { min-height:660px; display:grid; grid-template-columns:1.2fr .8fr; align-items:center; gap:60px; padding:62px 0 90px; }
    .eyebrow { color:var(--mint); font:700 12px/1 ui-monospace,SFMono-Regular,monospace; text-transform:uppercase; letter-spacing:.16em; }
    h1 { max-width:820px; margin:22px 0; font-size:clamp(54px,8vw,108px); line-height:.88; letter-spacing:-.075em; }
    .lede { max-width:640px; color:var(--muted); font-size:clamp(17px,2vw,21px); line-height:1.65; }
    .actions { display:flex; gap:12px; flex-wrap:wrap; margin-top:34px; }
    .orbit { aspect-ratio:1; border:1px solid var(--line); border-radius:50%; position:relative; display:grid; place-items:center;
      background:radial-gradient(circle,#122c37 0,#0b171f 56%,transparent 57%); box-shadow:0 0 80px #47dbbd14; }
    .orbit:before,.orbit:after { content:""; position:absolute; border:1px solid #2f5664; border-radius:50%; transform:rotate(-18deg); }
    .orbit:before { width:115%; height:38%; } .orbit:after { width:75%; height:24%; transform:rotate(35deg); }
    .planet { width:43%; aspect-ratio:1; border-radius:50%; background:linear-gradient(145deg,#8ff9db,#247c86 58%,#12313e); box-shadow:-18px 20px 50px #0008; }
    .status { position:absolute; bottom:7%; right:-2%; padding:12px 15px; border:1px solid var(--line); border-radius:12px;
      background:#0b171fee; color:var(--muted); font:12px ui-monospace,monospace; backdrop-filter:blur(12px); }
    .dot { display:inline-block; width:7px; height:7px; margin-right:8px; border-radius:50%; background:var(--mint); box-shadow:0 0 12px var(--mint); }
    section { padding:95px 0; border-top:1px solid var(--line); }
    .section-head { display:grid; grid-template-columns:.65fr 1.35fr; gap:40px; margin-bottom:42px; }
    h2 { margin:0; font-size:clamp(34px,5vw,62px); line-height:1; letter-spacing:-.055em; }
    .section-copy { margin:0; color:var(--muted); font-size:18px; line-height:1.7; }
    .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
    .card { min-height:230px; padding:26px; border:1px solid var(--line); border-radius:18px; background:linear-gradient(145deg,#10212c,#0b171f); }
    .number { color:var(--sun); font:12px ui-monospace,monospace; }
    .card h3 { margin:44px 0 10px; font-size:22px; letter-spacing:-.03em; }
    .card p { margin:0; color:var(--muted); line-height:1.6; }
    .install { display:flex; align-items:center; justify-content:space-between; gap:30px; padding:32px; border:1px solid #3c665e;
      border-radius:20px; background:linear-gradient(100deg,#102b2c,#101c25); }
    .install h3 { margin:0 0 8px; font-size:28px; } .install p { margin:0; color:var(--muted); }
    footer { padding:36px 0 48px; color:var(--muted); font-size:13px; display:flex; justify-content:space-between; }
    @media(max-width:820px) { nav a:not(.button){display:none}.hero{grid-template-columns:1fr;min-height:auto;padding-top:75px}.orbit{width:min(78vw,430px);margin:20px auto}
      .section-head{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.install{align-items:flex-start;flex-direction:column} }
    @media(prefers-reduced-motion:no-preference) { .orbit { animation:float 7s ease-in-out infinite; } @keyframes float{50%{transform:translateY(-12px)}} }
  </style>
</head>
<body>
  <div class="wrap">
    <header><a class="brand" href="/"><span class="mark"></span>Andromeda</a>
      <nav><a href="#system">The system</a><a href="/docs">API docs</a><a href="/admin">Operator console</a>
        <a class="button" href="https://github.com/akramboussanni/Andromeda.Installer/releases">Download</a></nav></header>
    <main>
      <div class="hero"><div><div class="eyebrow">Community-operated multiplayer</div>
        <h1>Back aboard.</h1><p class="lede">Andromeda reconnects Enemy On Board with private matchmaking, progression and dedicated game sessions — maintained in the open.</p>
        <div class="actions"><a class="button primary" href="https://github.com/akramboussanni/Andromeda.Installer/releases">Get the installer</a><a class="button" href="https://github.com/akramboussanni/Andromeda">View source</a></div></div>
        <div class="orbit" aria-hidden="true"><div class="planet"></div><div class="status"><span class="dot"></span>CORE ONLINE · v0.11.4</div></div></div>
      <section id="system"><div class="section-head"><div class="eyebrow">How it works</div><div><h2>One clean route back into the game.</h2><p class="section-copy">Install the compatibility mod once. Core handles player services and starts a dedicated game server only when a match needs one.</p></div></div>
        <div class="grid"><article class="card"><span class="number">01 / INSTALL</span><h3>Patch the client</h3><p>The guided installer locates your game, installs MelonLoader and keeps the Andromeda mod current.</p></article>
          <article class="card"><span class="number">02 / CONNECT</span><h3>Use community Core</h3><p>Accounts, parties, progression and matchmaking share a small, compatible control plane.</p></article>
          <article class="card"><span class="number">03 / PLAY</span><h3>Launch on demand</h3><p>Dedicated hosts start per session and close cleanly when the match is over.</p></article></div></section>
      <section><div class="install"><div><h3>Ready for another round?</h3><p>Windows installer · Open source · Community supported</p></div><a class="button primary" href="https://github.com/akramboussanni/Andromeda.Installer/releases">Download Andromeda</a></div></section>
    </main><footer><span>Andromeda is an independent community project.</span><span>Enemy On Board belongs to its respective owners.</span></footer>
  </div>
</body></html>"""
