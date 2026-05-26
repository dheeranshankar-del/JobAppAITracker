# JobAppAITracker

AI-powered Gmail job application tracker built in Python.

This project scans Gmail inbox data, detects internship and job application emails, classifies recruiting status using multiple LLMs through OpenRouter, and exports structured application data for tracking recruiting progress.

## Features

- Gmail API integration
- AI-powered email classification
- OpenRouter multi-model routing
- Support for Gemini, Grok, and Llama models
- Rule-based fallback classification
- Status detection:
  - Applied
  - Assessment Requested
  - Recruiter Contact
  - Interviewing
  - Final Round
  - Offer
  - Rejected
- Email caching for faster reruns
- Duplicate filtering
- JSON and CSV export
- Recruiter pipeline tracking
- Configurable keyword detection
- Batch email processing

## Tech Stack

- Python
- Gmail API
- OpenRouter API
- Gemini
- Grok
- Llama
- Pandas
- Requests

## How It Works

1. Authenticate with the Gmail API
2. Fetch recruiting-related emails from inbox data
3. Extract and preprocess email content
4. Route emails through AI classification models
5. Detect recruiting/application status
6. Export structured tracking results to JSON or CSV

## Files

- `JobAppAITracker.py` — main application script

## Future Improvements
- Faster Speed
- Dashboard UI
- Real-time monitoring
- Company name extraction
- Resume-to-job matching
- Analytics and visualizations
- Local vector search for applications
- Email summarization
- Web deployment

## Disclaimer

This project is my personal project you will need to buy you're own API keys in order for it to work
