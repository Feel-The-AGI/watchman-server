"""
Watchman Proposals Routes
Endpoints for LLM-powered proposal parsing and preview generation
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import Optional
from datetime import date
from loguru import logger

from app.database import Database
from app.middleware.auth import get_current_user, require_pro_tier
from app.engines.proposal_service import create_proposal_service
from app.engines.mutation_engine import create_mutation_engine
from app.engines.stats_engine import create_stats_engine


router = APIRouter()


class ParseInputRequest(BaseModel):
    text: str
    context: Optional[str] = None


class CreateProposalRequest(BaseModel):
    text: str
    auto_validate: bool = True


@router.post("/parse-pdf")
async def parse_pdf(
    file: UploadFile = File(...),
    user: dict = Depends(require_pro_tier)
):
    """
    Parse PDF content using Gemini's multimodal capabilities.
    Requires Pro tier.
    """
    # Validate file type
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(
            status_code=400,
            detail="Please upload a PDF file. Other formats aren't supported yet."
        )
    
    # Size limit: 10MB
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="PDF is a bit large. Please keep it under 10MB."
        )
    
    logger.info(f"User {user['id']} uploading PDF: {file.filename} ({len(contents)} bytes)")
    
    db = Database()
    proposal_service = create_proposal_service(user["id"])
    
    # Get user context
    commitments = await db.get_active_commitments(user["id"])
    cycle = await db.get_active_cycle(user["id"])
    
    context = {
        "active_commitments": [
            {"name": c.get("name"), "type": c.get("type")}
            for c in commitments
        ],
        "rotation_summary": f"Cycle: {cycle.get('name')}" if cycle else "No active cycle"
    }
    
    # Parse PDF via Gemini
    result = await proposal_service.parse_pdf(contents, context)
    
    if not result.get("success"):
        logger.warning(f"PDF parsing failed for user {user['id']}: {result.get('error')}")
        return {
            "success": False,
            "error": result.get("error", "Couldn't extract schedule from PDF"),
            "raw_response": result.get("raw_response")
        }
    
    logger.info(f"PDF parsed successfully for user {user['id']}")
    return {
        "success": True,
        "data": result.get("parsed"),
        "confidence": result.get("confidence", 0.8)
    }


@router.post("/parse")
async def parse_input(
    data: ParseInputRequest,
    user: dict = Depends(require_pro_tier)
):
    """
    Parse unstructured text input using LLM.
    Requires Pro tier.
    """
    db = Database()
    proposal_service = create_proposal_service(user["id"])
    
    # Build context from user's current state
    commitments = await db.get_active_commitments(user["id"])
    constraints = await db.get_active_constraints(user["id"])
    cycle = await db.get_active_cycle(user["id"])
    
    context = {
        "active_commitments": [
            {"name": c.get("name"), "type": c.get("type"), "status": c.get("status")}
            for c in commitments
        ],
        "constraints": [
            {"name": c.get("name"), "rule": c.get("rule")}
            for c in constraints
        ],
        "rotation_summary": f"Cycle: {cycle.get('name')}" if cycle else "No active cycle"
    }
    
    # Parse the input
    result = await proposal_service.parse_input(data.text, context)
    
    if not result.get("success"):
        return {
            "success": False,
            "error": result.get("error", "Failed to parse input"),
            "raw_response": result.get("raw_response")
        }
    
    return {
        "success": True,
        "data": result.get("parsed"),
        "confidence": result.get("confidence", 0.8)
    }


@router.post("/create")
async def create_proposal(
    data: CreateProposalRequest,
    user: dict = Depends(require_pro_tier)
):
    """
    Create a new proposal from text input.
    Parses the input, validates against constraints, and creates a mutation record.
    """
    db = Database()
    proposal_service = create_proposal_service(user["id"])
    mutation_engine = create_mutation_engine(user["id"])
    
    # Get user context
    commitments = await db.get_active_commitments(user["id"])
    constraints = await db.get_active_constraints(user["id"])
    cycle = await db.get_active_cycle(user["id"])
    
    # Parse the input - include cycle info in context
    context = {
        "rotation_summary": f"Cycle: {cycle.get('name')}" if cycle else "No active cycle",
        "active_commitments": [
            {"name": c.get("name"), "type": c.get("type"), "status": c.get("status")}
            for c in commitments
        ],
        "constraints": [
            {"name": c.get("name"), "rule": c.get("rule")}
            for c in constraints
        ]
    }
    
    parse_result = await proposal_service.parse_input(data.text, context)
    
    if not parse_result.get("success"):
        return {
            "success": False,
            "error": "Failed to parse input",
            "details": parse_result.get("error")
        }
    
    parsed = parse_result.get("parsed", {})
    
    # Validate if requested
    validation_result = {"is_valid": True, "violations": [], "alternatives": []}
    
    if data.auto_validate:
        # Get current calendar state
        year = date.today().year
        calendar_days = await db.get_calendar_days(
            user["id"],
            f"{year}-01-01",
            f"{year}-12-31"
        )
        
        validation_result = mutation_engine.validate_proposal(
            parsed,
            calendar_days,
            constraints,
            commitments
        )
    
    # Create mutation record
    mutation_data = mutation_engine.create_mutation_record(
        intent=parsed.get("intent", "unknown"),
        proposed_diff={
            "changes": parsed.get("changes", []),
            "summary": parsed.get("explanation", "")
        },
        explanation=proposal_service.generate_explanation(
            parsed,
            validation_result.get("violations"),
            validation_result.get("alternatives")
        ),
        violations=validation_result.get("violations"),
        alternatives=validation_result.get("alternatives"),
        source_text=data.text,
        triggered_by="llm"
    )
    
    mutation = await db.create_mutation(mutation_data)
    
    return {
        "success": True,
        "data": {
            "mutation": mutation,
            "is_valid": validation_result.get("is_valid", True),
            "violations": validation_result.get("violations", []),
            "alternatives": validation_result.get("alternatives", [])
        }
    }


@router.post("/preview")
async def preview_proposal(
    data: ParseInputRequest,
    user: dict = Depends(require_pro_tier)
):
    """
    Preview what a proposal would do without creating a mutation.
    """
    db = Database()
    proposal_service = create_proposal_service(user["id"])
    mutation_engine = create_mutation_engine(user["id"])
    stats_engine = create_stats_engine(user["id"])
    
    # Get user context
    commitments = await db.get_active_commitments(user["id"])
    constraints = await db.get_active_constraints(user["id"])
    
    context = {
        "active_commitments": [
            {"name": c.get("name"), "type": c.get("type")}
            for c in commitments
        ]
    }
    
    # Parse input
    parse_result = await proposal_service.parse_input(data.text, context)
    
    if not parse_result.get("success"):
        return {
            "success": False,
            "preview": {
                "is_valid": False,
                "explanation": "Failed to parse input",
                "violations": [],
                "alternatives": []
            }
        }
    
    parsed = parse_result.get("parsed", {})
    
    # Get calendar state for validation
    year = date.today().year
    calendar_days = await db.get_calendar_days(
        user["id"],
        f"{year}-01-01",
        f"{year}-12-31"
    )
    
    # Validate
    validation_result = mutation_engine.validate_proposal(
        parsed,
        calendar_days,
        constraints,
        commitments
    )
    
    # Calculate stats impact
    stats_impact = None
    if validation_result.get("is_valid"):
        current_stats = stats_engine.compute_yearly_stats(calendar_days, year)
        
        # Simulate applying changes
        simulated_state, _ = mutation_engine.apply_mutation(
            {"proposed_diff": parsed},
            calendar_days
        )
        
        new_stats = stats_engine.compute_yearly_stats(simulated_state, year)
        
        stats_impact = {
            "study_hours_change": new_stats.get("total_study_hours", 0) - current_stats.get("total_study_hours", 0),
            "overload_days_change": new_stats.get("overload_days_count", 0) - current_stats.get("overload_days_count", 0)
        }
    
    # Create preview
    preview = proposal_service.create_preview(
        parse_result,
        validation_result,
        stats_impact
    )
    
    return {
        "success": True,
        "preview": preview
    }


@router.post("/quick-add")
async def quick_add_commitment(
    name: str,
    type: str = "education",
    user: dict = Depends(get_current_user)
):
    """
    Quickly add a commitment without LLM parsing.
    Available for free tier.
    """
    db = Database()
    mutation_engine = create_mutation_engine(user["id"])
    
    # Check concurrent limits
    commitments = await db.get_active_commitments(user["id"])
    constraints = await db.get_active_constraints(user["id"])
    
    # Build a simple proposal
    proposal = {
        "intent": "add_commitment",
        "changes": [{
            "type": "add_commitment",
            "commitment": {
                "name": name,
                "type": type,
                "constraints_json": {
                    "study_on": ["off", "work_day_evening"],
                    "exclude": ["work_night"],
                    "frequency": "weekly",
                    "duration_hours": 2
                }
            }
        }],
        "explanation": f"Add {type} commitment: {name}"
    }
    
    # Get calendar for validation
    year = date.today().year
    calendar_days = await db.get_calendar_days(
        user["id"],
        f"{year}-01-01",
        f"{year}-12-31"
    )
    
    # Validate
    validation_result = mutation_engine.validate_proposal(
        proposal,
        calendar_days,
        constraints,
        commitments
    )
    
    # Create mutation
    mutation_data = mutation_engine.create_mutation_record(
        intent="add_commitment",
        proposed_diff={"changes": proposal["changes"]},
        explanation=proposal["explanation"],
        violations=validation_result.get("violations"),
        alternatives=validation_result.get("alternatives"),
        triggered_by="user"
    )
    
    mutation = await db.create_mutation(mutation_data)
    
    return {
        "success": True,
        "data": {
            "mutation": mutation,
            "is_valid": validation_result.get("is_valid"),
            "requires_review": not validation_result.get("is_valid"),
            "violations": validation_result.get("violations", []),
            "alternatives": validation_result.get("alternatives", [])
        }
    }
