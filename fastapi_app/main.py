# import logging
# import os
# import random
# import time
# from typing import Optional

# import httpx
# import uvicorn
# from fastapi import FastAPI, Response
# from opentelemetry.propagate import inject
# from utils import PrometheusMiddleware, metrics, setting_otlp

# APP_NAME = os.environ.get("APP_NAME", "app")
# EXPOSE_PORT = os.environ.get("EXPOSE_PORT", 8000)
# OTLP_GRPC_ENDPOINT = os.environ.get("OTLP_GRPC_ENDPOINT", "http://tempo:4317")

# TARGET_ONE_HOST = os.environ.get("TARGET_ONE_HOST", "app-b")
# TARGET_TWO_HOST = os.environ.get("TARGET_TWO_HOST", "app-c")

# app = FastAPI()

# # Setting metrics middleware
# app.add_middleware(PrometheusMiddleware, app_name=APP_NAME)
# app.add_route("/metrics", metrics)

# # Setting OpenTelemetry exporter
# setting_otlp(app, APP_NAME, OTLP_GRPC_ENDPOINT)


# class EndpointFilter(logging.Filter):
#     # Uvicorn endpoint access log filter
#     def filter(self, record: logging.LogRecord) -> bool:
#         return record.getMessage().find("GET /metrics") == -1


# # Filter out /endpoint
# logging.getLogger("uvicorn.access").addFilter(EndpointFilter())


# @app.get("/")
# async def read_root():
#     logging.error("Hello World")
#     return {"Hello": "World"}


# @app.get("/items/{item_id}")
# async def read_item(item_id: int, q: Optional[str] = None):
#     logging.error("items")
#     return {"item_id": item_id, "q": q}


# @app.get("/io_task")
# async def io_task():
#     time.sleep(1)
#     logging.error("io task")
#     return "IO bound task finish!"


# @app.get("/cpu_task")
# async def cpu_task():
#     for i in range(1000):
#         _ = i * i * i
#     logging.error("cpu task")
#     return "CPU bound task finish!"


# @app.get("/random_status")
# async def random_status(response: Response):
#     response.status_code = random.choice([200, 200, 300, 400, 500])
#     logging.error("random status")
#     return {"path": "/random_status"}


# @app.get("/random_sleep")
# async def random_sleep(response: Response):
#     time.sleep(random.randint(0, 5))
#     logging.error("random sleep")
#     return {"path": "/random_sleep"}


# @app.get("/error_test")
# async def error_test(response: Response):
#     logging.error("got error!!!!")
#     raise ValueError("value error")


# @app.get("/chain")
# async def chain(response: Response):
#     headers = {}
#     inject(headers)  # inject trace info to header
#     logging.critical(headers)

#     async with httpx.AsyncClient() as client:
#         await client.get(
#             "http://localhost:8000/",
#             headers=headers,
#         )
#     async with httpx.AsyncClient() as client:
#         await client.get(
#             f"http://{TARGET_ONE_HOST}:8000/io_task",
#             headers=headers,
#         )
#     async with httpx.AsyncClient() as client:
#         await client.get(
#             f"http://{TARGET_TWO_HOST}:8000/cpu_task",
#             headers=headers,
#         )
#     logging.info("Chain Finished")
#     return {"path": "/chain"}


# if __name__ == "__main__":
#     # update uvicorn access logger format
#     log_config = uvicorn.config.LOGGING_CONFIG
#     log_config["formatters"]["access"][
#         "fmt"
#     ] = "%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s resource.service.name=%(otelServiceName)s] - %(message)s"
#     uvicorn.run(app, host="0.0.0.0", port=EXPOSE_PORT, log_config=log_config)


import asyncio
import logging
import random
import time
from typing import List, Optional
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Response
from opentelemetry import trace
from opentelemetry.propagate import inject
from pydantic import BaseModel

from utils import PrometheusMiddleware, metrics, setting_otlp

# Configuration
APP_NAME = "ecommerce-service"
EXPOSE_PORT = 8000
OTLP_GRPC_ENDPOINT = "http://tempo:4317"

# Data models
class Product(BaseModel):
    id: str
    name: str
    price: float
    stock: int

class Order(BaseModel):
    id: str
    products: List[str]
    total: float
    status: str

# In-memory storage
products_db = {}
orders_db = {}

# Initialize FastAPI app with instrumentation
app = FastAPI()
app.add_middleware(PrometheusMiddleware, app_name=APP_NAME)
app.add_route("/metrics", metrics)
setting_otlp(app, APP_NAME, OTLP_GRPC_ENDPOINT)

# Simulate external payment service
async def process_payment(amount: float) -> bool:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("process_payment") as span:
        span.set_attribute("payment.amount", amount)
        await asyncio.sleep(random.uniform(0.1, 0.5))
        success = random.random() > 0.1  # 10% chance of failure
        span.set_attribute("payment.success", success)
        return success

@app.post("/products/", response_model=Product)
async def create_product(product: Product):
    logging.info(f"Creating new product: {product.name}")
    if product.id in products_db:
        raise HTTPException(status_code=400, detail="Product ID already exists")
    products_db[product.id] = product
    return product

@app.post("/orders/", response_model=Order)
async def create_order(product_ids: List[str]):
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("create_order") as span:
        order_id = str(uuid4())
        total = 0.0
        
        # Validate products and calculate total
        for pid in product_ids:
            if pid not in products_db:
                raise HTTPException(status_code=404, detail=f"Product {pid} not found")
            product = products_db[pid]
            if product.stock <= 0:
                raise HTTPException(status_code=400, detail=f"Product {pid} out of stock")
            total += product.price

        span.set_attribute("order.total", total)
        
        # Process payment
        payment_success = await process_payment(total)
        if not payment_success:
            logging.error(f"Payment failed for order {order_id}")
            raise HTTPException(status_code=400, detail="Payment failed")

        # Update inventory
        for pid in product_ids:
            products_db[pid].stock -= 1

        # Create order
        order = Order(
            id=order_id,
            products=product_ids,
            total=total,
            status="completed"
        )
        orders_db[order_id] = order
        
        logging.info(f"Order created successfully: {order_id}")
        return order

@app.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: str):
    if order_id not in orders_db:
        logging.error(f"Order not found: {order_id}")
        raise HTTPException(status_code=404, detail="Order not found")
    return orders_db[order_id]

@app.get("/stress-test")
async def stress_test():
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("stress_test") as span:
        # Simulate CPU-intensive task
        span.set_attribute("test.type", "stress")
        result = 0
        for i in range(1000000):
            result += i * random.random()
        return {"status": "completed", "result": result}

if __name__ == "__main__":
    import uvicorn
    
    # Configure logging format for tracing
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s resource.service.name=%(otelServiceName)s] - %(message)s"
    
    uvicorn.run(app, host="0.0.0.0", port=EXPOSE_PORT, log_config=log_config)