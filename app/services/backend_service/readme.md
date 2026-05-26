locally start backend service :
run this command from app/services/backend_service
PYTHONPATH=../.. uvicorn app:app --reload --host 0.0.0.0 --port 8000

using docker
run this from root 
docker build -f app/services/backend_service/Dockerfile .