"""Entry point for the CoachPops Streamlit dashboard.

Run locally (from the project root, with the venv activated):
    streamlit run app.py

Then command-click (or ctrl-click) the local URL it prints -- usually
http://localhost:8501 -- to open the dashboard in your browser.
"""

from ui.dashboard import main

if __name__ == "__main__":
    main()
