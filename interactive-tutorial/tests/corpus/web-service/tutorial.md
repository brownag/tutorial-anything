# Serve a tiny static site

1. Scaffold: create `site/index.html` with a one-line page.
2. Serve: start `python3 -m http.server 8000 --bind 127.0.0.1 --directory site` in its own terminal and leave it running.
3. Check: open `http://127.0.0.1:8000/` and confirm the page shows your line.
4. Stop: press Ctrl-C in the server terminal.
