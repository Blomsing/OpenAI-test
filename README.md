# Living City Ruler-RPG (Top-Down Strategy Prototype)

This repository now contains a playable browser prototype for a **city management + ruler RPG** game designed around your system sheet.

## What is implemented

- Top-down district map rendered on HTML5 canvas.
- 3 core resources: **Treasury**, **Influence**, **Personal Wealth**.
- 3 identity sliders: **Morality**, **Civic Culture** (Mercantile/Martial/Arcane), **Faith** (Order/Old Ways/Veiled).
- District leaders and district mood.
- City conditions: crime, hunger, unrest, corruption, magical instability.
- Weekly action categories (structural/political/situational/private blended into action buttons).
- 4-week month flow with monthly income refresh.
- Basic win/fail loop:
  - Win by surviving 12 months.
  - Lose if 3+ districts collapse into critical mood.

## Run locally

```bash
python -m http.server 8000
```

Open http://localhost:8000 and play in `index.html`.

## Steam-release readiness notes

This prototype is a **design vertical slice**, not full production. To be release-ready on Steam, next steps include:

- Engine migration (Unity, Godot, or Unreal) for packaging, save system, settings, input remapping.
- Data-driven content pipeline (JSON/ScriptableObjects): events, leaders, districts, projects, balancing.
- Full UX pass: tooltips, encyclopaedia, alerts, and decision history.
- Audio, localization, accessibility options, achievements.
- Narrative/event framework with authored arcs and replay variety.
- QA automation and build pipeline for Windows/macOS/Linux targets.

## Suggested V1 rules lock (next design step)

- Finalize exact district lineup for MVP.
- Lock weekly action count and action economy costs.
- Define culture and faith champions as city-wide actors.
- Create victory states by ideology route (examples: Arcane-Veiled hegemony, Benevolent Mercantile republic).
- Lock camera mode and interaction model for production.
