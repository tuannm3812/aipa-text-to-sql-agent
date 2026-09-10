"""Static configuration for the Streamlit UI: model choices and demo databases."""

from __future__ import annotations

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "Custom",
]

OLLAMA_MODELS = [
    "gemma3",
    "llama3.1",
    "llama3",
    "mistral",
    "qwen2.5",
    "Custom",
]

DEMO_DATABASES = {
    "University": {
        "path": "data/university_agent.db",
        "description": "Students, courses, grades, majors",
        "questions": [
            "How many students are enrolled in each major?",
            "What is the average score for each course?",
            "Which students have the highest average score?",
        ],
    },
    "Retail Analytics": {
        "path": "data/retail_analytics.db",
        "description": "Customers, orders, products, returns, stores",
        "questions": [
            "Show total completed sales revenue by customer region.",
            "Which return reasons occur most often?",
            "Which product categories generate the most revenue?",
        ],
    },
    "Healthcare Analytics": {
        "path": "data/healthcare_analytics.db",
        "description": "Patients, doctors, hospitals, appointments, treatments",
        "questions": [
            "How many appointments are there for each status?",
            "What is the average treatment cost by hospital city?",
            "Which specialties have the most completed appointments?",
        ],
    },
}
