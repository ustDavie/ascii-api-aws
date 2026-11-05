from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from jose import jwt
import boto3, os, time

app = FastAPI(title="ASCII Presigner API", version="1.0")

REGION = os.getenv("AWS_REGION", "ap-southeast-2")
BUCKET = os.getenv("S3_BUCKET", "ascii-api-video-store")
s3 = boto3.client("s3", region_name=REGION)

def get_username_from_auth(authorization: str):
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise ValueError("bad scheme")
        payload = jwt.decode(token, key=None, options={"verify_signature": False})
        return payload.get("cognito:username") or payload.get("username") or "anon"
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

class PresignRequest(BaseModel):
    filename: str
    content_type: str = "image/jpeg"

@app.get("/healthz")
def healthz():
    return "OK"

@app.post("/upload-url")
def create_upload_url(data: PresignRequest, authorization: str = Header(...)):
    user = get_username_from_auth(authorization)
    ts = int(time.time())
    key = f"{user}/input/{ts}-{data.filename}"
    url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": BUCKET, "Key": key, "ContentType": data.content_type},
        ExpiresIn=900  # 15 min
    )
    return {"upload_url": url, "s3_key": key, "bucket": BUCKET}
