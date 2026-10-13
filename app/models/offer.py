from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime, timezone

class Offer(BaseModel):
    retailer: str
    price: float
    original_price: Optional[float] = None
    url: str
    image_url: Optional[str] = None
    in_stock: bool = True
    title: Optional[str] = None
    condition: str = "new"
    shipping: float = 0.0
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    snippet: Optional[str] = None
    validated: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)

class ProductAnalysis(BaseModel):
    canonical_name: str
    category: str
    brand: str
    model: str
    specs: Dict[str, Any] = Field(default_factory=dict)
    target_price: Optional[float] = None
    retailer_offers: List[Offer] = Field(default_factory=list)

class PriceSnapshot(BaseModel):
    retailer_product_id: str
    price: float
    component_id: Optional[str] = None
    retailer: str = "Unknown"
    title: Optional[str] = None
    product_url: Optional[str] = None
    image_url: Optional[str] = None
    in_stock: bool = True
    condition: str = "new"
    shipping: float = 0.0
    category: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    recorded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class PriceCheckRequest(BaseModel):
    component_name: str
    component_id: Optional[str] = None
    category: Optional[str] = None
    retailers: Optional[List[str]] = None

class PriceCheckResponse(BaseModel):
    component_name: str
    component_id: Optional[str] = None
    best_price: float
    best_retailer: str
    offers: List[Offer]
    checked_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
