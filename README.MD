# MiniSocial: Coding Assignment 3

Eeli Tavaststjerna, Social Computing 2026–2027, University of Oulu.

Added features: five personalised post recommendations, introduction threads
(Chapter 5, Design Claim 19), and a private practice area
(Chapter 5, Design Claim 24). Assignment 1 and 2 features are included.
Developed with ChatGPT assistance.

## Run on Windows

Open PowerShell in the folder containing `app.py`:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:8080. Stop with Ctrl+C.
The bundled database is the original course demo. New columns are added automatically.
Local course prototype. Optional Docker files come from the original project and
were not tested for this submission; use the Windows instructions above.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use temporary databases. See `ohjeet/TESTAUS.txt` for results and manual checks.

Original project by Dr. Aku Visuri and Daniel Szabo:
https://github.com/Crowd-Computing-Oulu/mini_social_exercise

Claims: course Lecture/Exercise 6 (16.10.2026), slides 27–28, based on
Building Successful Online Communities, Chapter 5. Original license in `LICENSE`.
