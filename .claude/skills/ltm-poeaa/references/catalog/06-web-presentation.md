# Web Presentation Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 14.

Seven patterns. The first split is between **input controllers** (Page vs Front) and **view rendering** (Template vs Transform vs Two Step). MVC is the umbrella; Application Controller layers on top when navigation gets complex.

**For claude-ltm this whole category is mostly N/A.** claude-ltm is a **plugin**, not a web app — there is no HTTP application, no router, no SPA. The only presentation surface is the **read-only localhost viewer** (`viewer/`, stdlib `http.server`) that browses memory and the index across projects. The MCP server (`bin/mcp_server.py`) is a request dispatcher too, but it belongs to **Distribution** (the tool-call boundary), not Web Presentation. The verdicts below therefore judge each pattern against that one thin viewer, not a framework.

---

## Model View Controller (MVC)

> "Splits user interface interaction into three distinct roles."
> — <https://martinfowler.com/eaaCatalog/modelViewController.html>

**How it works.** Three roles: **Model** holds domain data and behaviour, **View** renders it, **Controller** receives input and orchestrates. The View depends on the Model; the Controller depends on both. The Model is unaware of the others.

**When to use.** Any UI with non-trivial interaction. Almost universal in web frameworks.

**claude-ltm applicability.** ⚠️ **Only loosely, in the viewer.** The viewer's request handler (controller), the `Store` rows it reads (model), and the HTML it emits (view) map onto the three roles, but there is no framework and no interaction to orchestrate — it is read-only. MVC is not a load-bearing umbrella here the way it is in a real web app; treat it as a description, not a design constraint.

---

## Page Controller

🪦 **Dated** — see `references/dated-patterns.md`. Superseded by Front Controller in modern REST API frameworks. Modern equivalent: Front Controller.

> "An object that handles a request for a specific page or action on a Web site."
> — <https://martinfowler.com/eaaCatalog/pageController.html>

**How it works.** One controller per page or action. Each URL maps to its own handler file/function. Cross-cutting behaviour is duplicated across them or extracted into helpers.

**When to use.** Simple sites with a small number of pages and minimal cross-cutting needs.

**When NOT to use.** Many endpoints with shared concerns (auth, logging, request shaping) — you'll duplicate that code per endpoint.

**Forbidden alongside.** Front Controller for the same routes.

**claude-ltm applicability.** ❌ Dated, not used. The viewer routes everything through one handler, not a controller per page.

---

## Front Controller

> "A controller that handles all requests for a Web site."
> — <https://martinfowler.com/eaaCatalog/frontController.html>

**How it works.** One handler at the top of the request pipeline receives every request, applies cross-cutting concerns (auth, instrumentation), then dispatches to a specific command/handler.

**When to use.** Any non-trivial site with cross-cutting concerns that must apply uniformly.

**Required pairings.** A Service Layer / command dispatcher to call into.

**Forbidden alongside.** Page Controller for the same routes.

**claude-ltm applicability.** ⚠️ **Front-Controller-shaped, but thin.** The viewer's stdlib `http.server` request handler dispatches every path from one entry point, and `bin/mcp_server.py`'s tool dispatch does the same for MCP tool calls (one dispatcher routes `recall`, `search_code`, `get_symbol`, …). Both fit the "one entry dispatches all requests" shape — but there is no framework, no middleware stack, and no cross-cutting pipeline; they are stdlib dispatchers over the `Store`. The shape is recognisable; the ceremony is not paid for.

---

## Template View

> "Renders information into HTML by embedding markers in an HTML page."
> — <https://martinfowler.com/eaaCatalog/templateView.html>

**How it works.** Author HTML directly with embedded markers (`{{ user.name }}`, `<%= ... %>`). The template engine resolves markers against a model.

**When to use.** Designers and developers share HTML — keeping markup intact matters.

**Forbidden alongside.** Transform View for the same page (pick one engine).

**claude-ltm applicability.** ⚠️ **Broadly Template-View-shaped, but minimal.** The viewer renders simple HTML directly from `Store` rows — there is no template engine, no marker language, just string assembly in Python. It resembles Template View only in that data goes in and HTML comes out; keep it that way rather than reaching for a templating dependency.

---

## Transform View

🪦 **Dated** — see `references/dated-patterns.md`. XSLT-era programmatic HTML emission. Modern equivalent: component-based rendering, which inherits the Template View shape.

> "A view that processes domain data element by element and transforms it into HTML."
> — <https://martinfowler.com/eaaCatalog/transformView.html>

**How it works.** Take the model as input, walk it, and emit HTML programmatically. Conceptually XSLT-shaped: data in, HTML out, no embedded markers in author-written HTML.

**When to use.** Multiple output formats from one model, or when the model shape is irregular and a template would be awkward.

**Forbidden alongside.** Template View for the same page.

**claude-ltm applicability.** ❌ Dated, not used. The viewer is single-format HTML.

---

## Two Step View

🪦 **Dated as full ceremony** — see `references/dated-patterns.md`. Rarely worth the implementation overhead outside a large multi-format UI.

> "Turns domain data into HTML in two steps: first by forming some kind of logical page, then rendering the logical page into HTML."
> — <https://martinfowler.com/eaaCatalog/twoStepView.html>

**How it works.** Step 1: build a logical, format-agnostic representation of the page (sections, fields, tables). Step 2: render that representation as HTML. Global look-and-feel changes happen in step 2 only.

**When to use.** Many pages must share consistent appearance, or the system supports multiple output formats (HTML + PDF + JSON).

**claude-ltm applicability.** ❌ Not used. The viewer is single-format, minimal HTML — there is no logical-page layer and no second rendering step to pay for.

---

## Application Controller

> "A centralized point for handling screen navigation and the flow of an application."
> — <https://martinfowler.com/eaaCatalog/applicationController.html>

**How it works.** A separate controller knows the application's flow — wizard steps, conditional navigation, post-action redirects — and tells the input controllers what view to render and what command to run next. Reduces duplicated navigation logic across controllers.

**When to use.** Wizard flows, multi-step processes, or any UI where the next screen depends on application state rather than a single user click.

**Required pairings.** Front Controller (or the application controller becomes its own dispatch point).

**claude-ltm applicability.** ❌ Not used — there is no screen navigation or application flow. The viewer is a read-only browser with no wizards, no multi-step state, nothing to sequence.

---

## Choosing among the seven

For claude-ltm, Web Presentation is essentially **N/A**. There is no web application to structure — only a thin **Front-Controller-shaped dispatcher** (the localhost `viewer/` and `bin/mcp_server.py`'s tool dispatch) sitting over the `Store`, emitting **minimal HTML** directly (broadly Template-View-shaped), with no framework, no templating engine, no navigation flow, and nothing more. Page Controller, Transform View, Two Step View and Application Controller are all unused. See `references/ltm-defaults.md` § Web Presentation.
