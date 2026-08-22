# CoachPops — Fantasy Football Manager Dashboard

A Python-based dashboard for managing a fantasy football team, built with
[Streamlit](https://streamlit.io/), [pandas](https://pandas.pydata.org/), and
[yfpy](https://github.com/uberfastman/yfpy) (Yahoo Fantasy Sports API wrapper).

## Project Structure

- `api/` — data-fetching and integration code (e.g. Yahoo Fantasy Sports API via yfpy)
- `data/` — local data storage, caches, and processed datasets
- `ui/` — Streamlit dashboard pages and UI components

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
