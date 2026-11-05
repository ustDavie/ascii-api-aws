from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Header
from fastapi.responses import PlainTextResponse
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from jose import jwt, JWTError
from pydantic import BaseModel
from datetime import datetime, timedelta
from boto3.dynamodb.conditions import Key
import boto3
from botocore.exceptions import NoCredentialsError, ClientError
import io
import json
import hmac
import hashlib
import base64

# === App Setup ===
app = FastAPI()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# === AWS Clients ===
region = "ap-southeast-2"
ssm = boto3.client("ssm", region_name=region)
dynamodb = boto3.resource("dynamodb", region_name=region)
sqs = boto3.client("sqs", region_name=region)
s3 = boto3.client("s3")
secretsmanager = boto3.client("secretsmanager", region_name=region)

# === AWS Config ===
queue_url = ssm.get_parameter(Name="/ascii-api/queue-url")['Parameter']['Value']
table_name = ssm.get_parameter(Name="/ascii-api/table-name")['Parameter']['Value']
table = dynamodb.Table(table_name)

COGNITO_REGION = region
COGNITO_USER_POOL_ID = "ap-southeast-2_dWQG6rVun"
COGNITO_CLIENT_ID = "3f6d2khmhcb1flchs3pndn0gii"
COGNITO_CLIENT_SECRET = "18p514dl3j9bmacvfsap7o8fksidos1j6k4ruo4brcrmnftecjnm"

SECRET_KEY = "mysecret"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# === Models ===
class LoginForm(BaseModel):
    username: str
    password: str

# === Helper Functions ===
def calculate_secret_hash(username, client_id, client_secret):
    message = username + client_id
    dig = hmac.new(client_secret.encode(), msg=message.encode(), digestmod=hashlib.sha256).digest()
    return base64.b64encode(dig).decode()

def create_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_secret(name):
    try:
        return secretsmanager.get_secret_value(SecretId=name)["SecretString"]
    except Exception as e:
        print(f"❌ Error getting secret: {e}")
        return None

# === Authentication Dependencies ===
def get_user_payload(token: str = Depends(oauth2_scheme)):
    try:
        return jwt.decode(token, key=None, options={"verify_signature": False})
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(authorization: str = Header(...)):
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authentication scheme.")
        
        # Decode without verifying signature — for demo/testing only
        payload = jwt.get_unverified_claims(token)

        username = payload.get("username") or payload.get("cognito:username")
        if not username:
            raise HTTPException(status_code=401, detail="Username not found in token.")
        
        return username
    except (ValueError, JWTError):
        raise HTTPException(status_code=401, detail="Invalid token.")


def require_admin(payload: dict = Depends(get_user_payload)):
    groups = payload.get("cognito:groups", [])
    if "admin" not in groups:
        raise HTTPException(status_code=403, detail="Admins only")
    return payload

# === Health ===
@app.get("/healthz")
def health_check():
    return PlainTextResponse("OK", status_code=200)


# === Auth Routes ===
@app.post("/login")
def login_user(username: str = Form(...), password: str = Form(...)):
    client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
    try:
        secret_hash = calculate_secret_hash(username, COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET)
        response = client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow='USER_PASSWORD_AUTH',
            AuthParameters={
                'USERNAME': username,
                'PASSWORD': password,
                'SECRET_HASH': secret_hash
            }
        )

        if response.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
            return {
                "challenge": "NEW_PASSWORD_REQUIRED",
                "session": response["Session"],
                "message": "User must set a new password"
            }

        return {
            "access_token": response['AuthenticationResult']['AccessToken'],
            "id_token": response['AuthenticationResult']['IdToken'],
            "refresh_token": response['AuthenticationResult']['RefreshToken']
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/respond-to-challenge")
def respond_to_challenge(username: str = Form(...), new_password: str = Form(...), session: str = Form(...)):
    client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
    try:
        secret_hash = calculate_secret_hash(username, COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET)
        response = client.respond_to_auth_challenge(
            ClientId=COGNITO_CLIENT_ID,
            ChallengeName='NEW_PASSWORD_REQUIRED',
            Session=session,
            ChallengeResponses={
                'USERNAME': username,
                'NEW_PASSWORD': new_password,
                'SECRET_HASH': secret_hash
            }
        )
        return {
            "message": "Password updated successfully.",
            "access_token": response['AuthenticationResult']['AccessToken'],
            "id_token": response['AuthenticationResult']['IdToken'],
            "refresh_token": response['AuthenticationResult']['RefreshToken']
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# === Main Route: Convert ===
@app.post("/convert")
def convert_image(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        username = current_user['username']
        print("🔐 Authenticated user:", username)

        # Save uploaded file to /tmp
        temp_input_path = f"/tmp/{file.filename}"
        with open(temp_input_path, "wb") as f:
            content = file.file.read()
            f.write(content)
        print("📥 File saved to:", temp_input_path)

        # Generate S3 keys
        s3_input_key = f"{username}/input/{file.filename}"
        s3_output_key = f"{username}/output/{file.filename}.txt"

        # Upload to S3
        bucket = 'ascii-api-video-store'
        s3.upload_file(temp_input_path, bucket, s3_input_key)
        print("☁️ Uploaded to S3:", s3_input_key)

        # Send message to SQS
        message = {
            "username": username,
            "s3_input_key": s3_input_key,
            "s3_output_key": s3_output_key,
            "original_filename": file.filename
        }
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))
        print("📤 Message sent to SQS:", message)

        return {"message": "File uploaded and processing started"}

    except Exception as e:
        print("❌ Error in /convert:", str(e))
        raise HTTPException(status_code=500, detail="Internal Server Error")

# === Results ===
@app.get("/results")
def get_metadata(username: str = Depends(get_current_user)):
    try:
        response = table.query(
            KeyConditionExpression=Key("qut-username").eq(username)
        )
        return response.get("Items", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# === Secret Demo ===
@app.get("/secret-demo")
def show_secret():
    return json.loads(get_secret("ascii-api/secret-demo"))

# === Cognito Register ===
@app.post("/register")
def register_user(username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
    try:
        secret_hash = calculate_secret_hash(username, COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET)
        client.sign_up(
            ClientId=COGNITO_CLIENT_ID,
            Username=username,
            Password=password,
            SecretHash=secret_hash,
            UserAttributes=[{"Name": "email", "Value": email}]
        )
        return {"message": "Registration successful. Please confirm via email."}
    except client.exceptions.UsernameExistsException:
        raise HTTPException(status_code=400, detail="Username already exists")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/confirm")
def confirm_registration(username: str = Form(...), code: str = Form(...)):
    client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
    try:
        secret_hash = calculate_secret_hash(username, COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET)
        client.confirm_sign_up(
            ClientId=COGNITO_CLIENT_ID,
            Username=username,
            ConfirmationCode=code,
            SecretHash=secret_hash
        )
        return {"message": "User confirmed"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# === Extra Routes ===
@app.get("/secure-route")
def secure_endpoint(token: str = Depends(oauth2_scheme)):
    payload = jwt.decode(token, key=None, options={"verify_signature": False})
    username = payload.get("username") or payload.get("cognito:username")
    return {"message": f"Hello {username}, you're authenticated!"}

@app.get("/admin")
def admin_only_route(payload: dict = Depends(require_admin)):
    username = payload.get("username") or payload.get("cognito:username")
    return {"message": f"Welcome, admin {username}!"}

@app.get("/whoami")
def whoami(token: str = Depends(oauth2_scheme)):
    payload = jwt.decode(token, key=None, options={"verify_signature": False})
    return {
        "username": payload.get("cognito:username") or payload.get("username"),
        "groups": payload.get("cognito:groups", [])
    }
