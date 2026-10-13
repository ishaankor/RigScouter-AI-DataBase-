import re
from typing import Tuple, List, Optional
from app.models.offer import ProductAnalysis

def validate_offer_fast(
    title: str,
    price: float,
    analysis: Optional[ProductAnalysis] = None
) -> Tuple[bool, str]:
    """
    Lightning-fast heuristic check to filter out accessories, toys,
    prebuilts, cables, and wrong hardware tiers.
    """
    if price <= 0:
        return False, "Price <= 0"

    t_lower = (title or "").lower()

    # Universal spam / non-hardware keywords
    spam_terms = [
        "keychain", "poster", "t-shirt", "mug", "decal", "sticker",
        "dummy model", "display model", "replica", "toy", "box only",
        "empty box", "paper edition", "picture only"
    ]
    for term in spam_terms:
        if term in t_lower:
            return False, f"Matches spam keyword: '{term}'"

    # Category / Model specific validation if analysis provided
    if analysis:
        # Negative keywords from product analysis
        negatives = analysis.specs.get("negative_keywords", [])
        for neg in negatives:
            if neg.lower() in t_lower:
                return False, f"Matches negative keyword: '{neg}'"

        # Price floor check (rejects waterblocks/cables masquerading as GPUs/CPUs)
        min_price = analysis.target_price or analysis.specs.get("min_price")
        if min_price and float(min_price) > 0:
            floor = float(min_price) * 0.35
            if price < floor:
                return False, f"Price ${price:.2f} is below minimum hardware floor (${floor:.2f})"

        # Model specificity checks
        # Prevent e.g. "RTX 4070" from matching "RTX 4070 Ti" or "RTX 4070 Super" if target didn't specify Ti/Super
        model_clean = analysis.model.lower()
        if "super" not in model_clean and " super" in t_lower:
            return False, "Candidate is Super variant, but target is non-Super"
        if "ti" not in model_clean and (" ti " in t_lower or " ti-" in t_lower or t_lower.endswith(" ti")):
            return False, "Candidate is Ti variant, but target is non-Ti"
        if "xtx" not in model_clean and "xtx" in t_lower:
            return False, "Candidate is XTX variant, but target is non-XTX"
        if "xt" not in model_clean and (" xt " in t_lower or t_lower.endswith(" xt")):
            return False, "Candidate is XT variant, but target is non-XT"

    return True, "Valid offer"
