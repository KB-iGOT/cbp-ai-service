# iGOT AI-Driven CBP Training Plan Creation System

A FastAPI service for managing training centers and organizations for Competency-Based Program (CBP) development with AI-driven training plan creation capabilities.

## Project Overview

This system is designed to support AI-driven Competency-Based Program (CBP) training plan creation by managing the foundational infrastructure of training centers and organizations. It provides APIs for registering and managing educational institutions and training organizations that will be used for CBP delivery.

## Project Structure

```
src/
├── main.py          # FastAPI application with CBP-focused API endpoints
├── config.py        # Configuration settings with CBP-specific options
├── schemas.py       # Pydantic schemas for training centers and organizations
└── models.py        # SQLAlchemy models for CBP infrastructure
requirements.txt     # Python dependencies for CBP system
docker-compose.yml   # Docker setup for CBP development environment
Dockerfile          # Container configuration
README.md           # This documentation
```

## Key Features

- **Role Mapping Management**: Create and manage role mappings with competencies
- **AI-Powered Course Recommendations**: Generate course suggestions using vector embeddings
- **CBP Plan Creation**: Save user-selected courses as comprehensive training plans
- **Document Processing**: Upload and process ACBP plans and work allocation orders
- **Multi-Organization Support**: Manage state/center and department-level data

## Tech Stack

- **Framework**: FastAPI
- **Database**: PostgreSQL
- **ORM**: SQLAlchemy
- **AI Integration**: Google Gemini 2.5 Pro
- **Vector Search**: pgvector extension

## Quick Start

### Prerequisites

```bash
# Required
- Python 3.12+
- PostgreSQL 14+
- pgvector extension
```

### Installation

```bash
# Clone repository
git clone https://github.com/KB-iGOT/cbp-ai-service.git
cd cbp-ai-service

# Install dependencies
uv sync

# Create `.env.` file and Set environment variables
```

### Environment Variables

```bash
# Database
DATABASE_URL=postgresql://user:password@localhost/dbname

# Google AI
GOOGLE_PROJECT_ID=your-project-id
GOOGLE_APPLICATION_CREDENTIALS=path/to/credentials.json
EMBEDDING_MODEL_NAME=text-multilingual-embedding-002

# Knowledge Base API
KB_BASE_URL=https://your-kb-api.com/v1
KB_AUTH_TOKEN=your-kb-token

# File Limits
PDF_MAX_FILE_SIZE=52428800  # 50MB
CSV_MAX_FILE_SIZE=10485760  # 10MB
```

### Run Application

```bash
# Start server
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# Access API docs
# http://localhost:8000/docs
```
## API Documentation

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`
- **OpenAPI JSON**: `http://localhost:8000/openapi.json`

