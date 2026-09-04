"""Add Tier 2 customisations to the local Odoo, over XML-RPC.

A stock Odoo is all Tier 1: every table is documented and almost certainly memorised by any LLM.
The interesting half of the problem is what a consultant bolted on in 2011 -- models and fields
with terse, abbreviated names and labels that explain nothing.

This creates, the way Odoo Studio does (``state='manual'``):

  * two custom models   x_sup_qual_rec, x_mfg_scrap
  * two custom fields on standard models (res.partner, product.template)
  * a few rows in each, so the columns have sample values to reason about

Usage:  python docker/seed_custom_models.py [--url http://localhost:8069] [--db erp_planner]
"""

from __future__ import annotations

import argparse
import random
import xmlrpc.client

MODELS = [
    {
        "model": "x_sup_qual_rec",
        "name": "Sup Qual Rec",  # terse on purpose - this is what real customisations look like
        "fields": [
            ("x_prt", "Prt", "many2one", "res.partner"),
            ("x_dt", "Dt", "date", None),
            ("x_scr", "Scr", "float", None),
            ("x_nc_cnt", "NC Cnt", "integer", None),
            ("x_aud_by", "Aud By", "many2one", "res.users"),
        ],
    },
    {
        "model": "x_mfg_scrap",
        "name": "ZSCRAP",
        "fields": [
            ("x_tmpl", "Tmpl", "many2one", "product.template"),
            ("x_qty", "Qty", "float", None),
            ("x_rsn", "Rsn", "char", None),
            ("x_shift", "Shift", "char", None),
        ],
    },
]

# Odoo Studio-style fields added to tables Odoo itself ships.
EXTRA_FIELDS = [
    ("res.partner", "x_cust_seg", "Cust Seg", "char", None),
    ("res.partner", "x_cr_lim", "Cr Lim", "float", None),
    ("product.template", "x_abc_cls", "ABC Cls", "char", None),
]


class Odoo:
    def __init__(self, url: str, db: str, user: str, password: str) -> None:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        self.uid = common.authenticate(db, user, password, {})
        if not self.uid:
            raise SystemExit(f"authentication failed for {user!r} on {db!r}")
        self.proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        self.db, self.password = db, password

    def call(self, model: str, method: str, *args, **kwargs):
        return self.proxy.execute_kw(self.db, self.uid, self.password, model, method, list(args), kwargs)

    def find_or_create(self, model: str, domain: list, values: dict) -> int:
        found = self.call(model, "search", domain, limit=1)
        return found[0] if found else self.call(model, "create", values)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8069")
    parser.add_argument("--db", default="erp_planner")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin")
    args = parser.parse_args()

    odoo = Odoo(args.url, args.db, args.user, args.password)
    random.seed(7)

    for spec in MODELS:
        model_id = odoo.find_or_create(
            "ir.model",
            [("model", "=", spec["model"])],
            {"name": spec["name"], "model": spec["model"], "state": "manual"},
        )
        for name, label, ttype, relation in spec["fields"]:
            values = {
                "model_id": model_id,
                "name": name,
                "field_description": label,
                "ttype": ttype,
                "state": "manual",
            }
            if relation:
                values["relation"] = relation
            odoo.find_or_create(
                "ir.model.fields",
                [("model", "=", spec["model"]), ("name", "=", name)],
                values,
            )
        # A manual model has no access rules until someone makes them; without this every
        # read/write below fails with "No group currently allows this operation".
        odoo.find_or_create(
            "ir.model.access",
            [("model_id", "=", model_id)],
            {
                "name": f"access_{spec['model']}",
                "model_id": model_id,
                "perm_read": True,
                "perm_write": True,
                "perm_create": True,
                "perm_unlink": True,
            },
        )
        print(f"model {spec['model']}: ok")

    for model, name, label, ttype, relation in EXTRA_FIELDS:
        model_id = odoo.call("ir.model", "search", [("model", "=", model)], limit=1)[0]
        values = {
            "model_id": model_id,
            "name": name,
            "field_description": label,
            "ttype": ttype,
            "state": "manual",
        }
        if relation:
            values["relation"] = relation
        odoo.find_or_create("ir.model.fields", [("model", "=", model), ("name", "=", name)], values)
        print(f"field {model}.{name}: ok")

    # --- rows, so the columns have values to infer from ---------------------------------------
    partners = odoo.call("res.partner", "search", [("supplier_rank", ">", 0)], limit=8) or odoo.call(
        "res.partner", "search", [], limit=8
    )
    users = odoo.call("res.users", "search", [], limit=3)
    products = odoo.call("product.template", "search", [], limit=10)

    if odoo.call("x_sup_qual_rec", "search_count", []) == 0:
        for i in range(24):
            odoo.call(
                "x_sup_qual_rec",
                "create",
                {
                    "x_name": f"QA-{2000 + i}",
                    "x_prt": random.choice(partners),
                    "x_dt": f"2026-0{random.randint(1, 8)}-{random.randint(10, 28)}",
                    "x_scr": round(random.uniform(1.0, 5.0), 1),
                    "x_nc_cnt": random.randint(0, 6),
                    "x_aud_by": random.choice(users),
                },
            )
        print("x_sup_qual_rec: 24 rows")

    if odoo.call("x_mfg_scrap", "search_count", []) == 0:
        for i in range(40):
            odoo.call(
                "x_mfg_scrap",
                "create",
                {
                    "x_name": f"SCR-{5000 + i}",
                    "x_tmpl": random.choice(products),
                    "x_qty": round(random.uniform(0.5, 30.0), 1),
                    "x_rsn": random.choice(["TOL", "SURF", "OPER", "MATL"]),
                    "x_shift": random.choice(["A", "B", "C"]),
                },
            )
        print("x_mfg_scrap: 40 rows")

    for pid in partners:
        odoo.call(
            "res.partner",
            "write",
            [pid],
            {"x_cust_seg": random.choice(["KA", "MM", "SMB"]), "x_cr_lim": random.choice([5000.0, 25000.0])},
        )
    for tid in products:
        odoo.call("product.template", "write", [tid], {"x_abc_cls": random.choice(["A", "B", "C"])})
    print("studio fields populated")


if __name__ == "__main__":
    main()
