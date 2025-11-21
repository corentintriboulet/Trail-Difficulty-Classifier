# Trail-Difficulty-Classifier

**Classifies trail difficulty (1-5) using Strava segment data.**

### Why?
- Help cyclists/runners pick trails matching their skill level or avoid trails that are too difficult
- Personal project to practice ML and data engineering

---

### Difficulty Scale

| Score | Surface Type       | Description                     |
|-------|--------------------|---------------------------------|
| 1     | Road/Paved         | Smooth, no obstacles.           |
| 2     | Gravel/Compact Dirt| Minor bumps, easy to navigate.  |
| 3     | Mixed Terrain      | Some rocks/roots, moderate.     |
| 4     | Technical Singletrack | Frequent obstacles, skill needed. |
| 5     | Extreme/Unrideable | Heavy rocks/roots, expert only. |

---

### Repository Structure

````bash
Trail-Difficulty-Classifier/
├── data/
│   ├── processed/          # Cleaned datasets (e.g., segments_manually_labeled.csv)
│   └── raw/                # Original Strava data (ignored by Git)
├── notebooks/
│   └── explorations.ipynb  # EDA and baseline models
└── src/
    ├── data/               # Data loading scripts
    └── models/             # ML code
````

### Setup 
1. Add Strava API keys to config.yaml (see .gitignore).
2. Install `requirements.txt`