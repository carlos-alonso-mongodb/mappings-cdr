#!/usr/bin/env python3
import os
import sys
import json
import logging
import time
import certifi
from pymongo import MongoClient, UpdateOne

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def load_config(config_file):
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_mappings(mappings_file):
    with open(mappings_file, "r", encoding="utf-8") as f:
        return json.load(f)


def match_ancestors(node_ancestors, required_ancestors):
    if not required_ancestors:
        return True
    i = 0
    for anc in node_ancestors:
        if anc == required_ancestors[i]:
            i += 1
            if i == len(required_ancestors):
                return True
    return False


def get_value_by_path(data_dict, path_segments):
    cur = data_dict
    for seg in path_segments:
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def nest_value(segments, val):
    nested = {}
    current = nested
    for seg in segments[:-1]:
        current[seg] = {}
        current = current[seg]
    current[segments[-1]] = val
    return nested


def merge_dicts(a, b):
    for key, val in b.items():
        if key in a and isinstance(a[key], dict) and isinstance(val, dict):
            a[key] = merge_dicts(a[key], val)
        else:
            a[key] = val
    return a


def build_new_sn_from_cn(cn_array, new_mapping):
    sn_version = new_mapping.get("snv", 1)
    mapping_rules = new_mapping.get("mappings", [])
    new_sn = []

    for rule in mapping_rules:
        desired_ai       = rule.get("archetype_node_id")
        required_ancestors = rule.get("ancestors", [])
        exact_match      = rule.get("exactAncestorsMatch", False)
        node_type        = rule.get("node_type")
        create_ancestors = str(rule.get("createAncestorsArray", "false")).lower() == "true"
        data_paths       = rule.get("data", [])

        for node in cn_array:
            node_data      = node.get("d", {})
            node_ai        = node_data.get("ani")
            node_ancestors = node.get("a", [])

            # 1) filter by node_type if provided
            if node_type and node_data.get("T") != node_type:
                continue

            # 2) wildcard or specific archetype match
            if desired_ai != "*" and node_ai != desired_ai:
                continue

            # 3) ancestor matching
            if exact_match:
                if node_ancestors != required_ancestors:
                    continue
            else:
                if not match_ancestors(node_ancestors, required_ancestors):
                    continue

            # 4) build output node
            out = {}
            if create_ancestors and node_ancestors:
                out["a"] = node_ancestors

            d_out = {}
            for path_expr in data_paths:
                segments = [p for p in path_expr.split('/') if p]
                val = get_value_by_path(node_data, segments)
                if val is not None:
                    nested = nest_value(segments, val)
                    d_out = merge_dicts(d_out, nested)

            if d_out:
                out["d"] = d_out
                new_sn.append(out)

    return {"version": sn_version, "nodes": new_sn}


def main():
    if len(sys.argv) < 3:
        print("Usage: python delta-updater.py <config.json> <mappings.json>")
        sys.exit(0)

    config_file   = sys.argv[1]
    mappings_file = sys.argv[2]
    config        = load_config(config_file)
    mappings_def  = load_mappings(mappings_file)

    connection_string = config["connection_string"]
    db_name           = config["db_name"]
    coll_name         = config["collection_name"]
    batch_size        = config.get("batch_size", 1000)

    ca_path = certifi.where()
    client  = MongoClient(connection_string, tlsCAFile=ca_path)
    db      = client[db_name]
    coll    = db[coll_name]

    # Iterate over each archetype mapping
    for top_archetype, new_mapping in mappings_def.items():
        if new_mapping.get("skip_mapping", False):
            logging.info(f"Skipping archetype '{top_archetype}' (skip_mapping=true).")
            continue
        clear_mapping        = new_mapping.get("clear_mapping", False)
        start_time           = time.time()
        new_snv              = new_mapping.get("snv", 1)
        required_template_version = new_mapping.get("template_version")
        force_update         = new_mapping.get("force_update", False)

        logging.info(
            f"Processing archetype '{top_archetype}' (template_version={required_template_version}, new snv={new_snv}, remove_fields={clear_mapping})"
        )

        # Build filter query
        base_filter = {
            "cn.d.ani": top_archetype,
            "version": required_template_version
        }
        if not force_update:
            base_filter["$or"] = [
                {"snv": {"$exists": False}},
                {"snv": {"$lt": new_snv}}
            ]

        total_docs = coll.count_documents(base_filter)
        logging.info(f"Found {total_docs} matching documents for '{top_archetype}'.")

        cursor = coll.find(base_filter, projection=["cn", "sn", "snv", "version"])
        buffer = []
        updates_count = 0
        processed_docs = 0

        for doc in cursor:
            processed_docs += 1
            _id = doc["_id"]

            if clear_mapping:
                update_op = {"$unset": {"sn": "", "snv": ""}}
            else:
                cn_array   = doc.get("cn", [])
                new_sn_obj = build_new_sn_from_cn(cn_array, new_mapping)
                updated_snv = new_sn_obj["version"]
                new_sn      = new_sn_obj["nodes"]
                update_op   = {"$set": {"sn": new_sn, "snv": updated_snv}}

            buffer.append({"filter": {"_id": _id}, "update": update_op})

            # flush batch
            if len(buffer) >= batch_size:
                ops = [UpdateOne(bu["filter"], bu["update"]) for bu in buffer]
                result = coll.bulk_write(ops, ordered=False)
                updates_count += result.modified_count
                logging.info(f"Processed {processed_docs}/{total_docs}. Batch updated: {result.modified_count} docs.")
                buffer.clear()

        # final batch
        if buffer:
            ops = [UpdateOne(bu["filter"], bu["update"]) for bu in buffer]
            result = coll.bulk_write(ops, ordered=False)
            updates_count += result.modified_count
            logging.info(f"Final batch for '{top_archetype}'. Updated: {result.modified_count} docs.")
            buffer.clear()

        elapsed = time.time() - start_time
        logging.info(
            f"Finished '{top_archetype}': total modified={updates_count}, time={elapsed:.2f}s"
        )

    logging.info("All done.")
    client.close()


if __name__ == "__main__":
    main()
