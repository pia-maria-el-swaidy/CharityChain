# TraceChain API

## Gateway — Port 8000

### GET /health
Returns: `{"status": "gateway ok"}`

### POST /projects
Create a project in DB.
Body: `{"name": "Food for Beirut", "description": "test", "target_amount": 1000}`
Returns: `{"status": "created", "project": {...}}`

### GET /projects
List all projects from DB.
Returns: `{"projects": [{"id": 1, "name": "...", ...}]}`

### POST /allocations
Create allocation with project name as memo.
Body: `{"project_name": "Food for Beirut", "amount": 100}`
Returns: `{"status": "allocation created", "memo": "Food for Beirut", "amount": 100}`

### POST /donations
TODO: Forward to validator node.
Body: `{"donor": "...", "project_name": "...", "amount": 100}`

### GET /trace
TODO: Read /trace from validator node.

## Validator / Blockchain — Port 5000

### POST /mine
TODO: later

### GET /trace
TODO: later

### POST /blocks/propose
TODO: later