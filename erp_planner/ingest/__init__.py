"""Phase 1 -- ingestion connectors."""

from erp_planner.ingest.odoo import OdooFieldMeta, OdooModelMeta, fetch_odoo_metadata, ingest_odoo

__all__ = ["OdooFieldMeta", "OdooModelMeta", "fetch_odoo_metadata", "ingest_odoo"]
