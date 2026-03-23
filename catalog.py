import json
import os
from typing import List, Dict, Any
from models import CharacterData, ItemData, AbilityData, PerkData, SkinData

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CATALOG_PATH = os.path.join(DATA_DIR, "catalog.json")

class Catalog:
    _data: Dict[str, Any] = {}

    @classmethod
    def load(cls):
        # Determine if we are using split files or single file
        # Priority: Split files in DATA_DIR
        
        # Helper to load a file safely
        def load_file(filename):
            # Check constant_data subfolder first
            path = os.path.join(DATA_DIR, "constant_data", filename)
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return json.load(f)
            
            # Fallback to data root
            path = os.path.join(DATA_DIR, filename)
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return json.load(f)
            return [] # Return empty list if missing

        # Load each category
        characters = load_file("characters.json")
        items = load_file("items.json")
        abilities = load_file("abilities.json")
        perks = load_file("perks.json")
        skins = load_file("skins.json")
        # Progression might be a dict, not a list
        progression_path = os.path.join(DATA_DIR, "progression.json")
        progression = {}
        if os.path.exists(progression_path):
             with open(progression_path, 'r') as f:
                  progression = json.load(f)

        # If we found nothing, try legacy catalog.json
        if not characters and not items:
            if os.path.exists(CATALOG_PATH):
                with open(CATALOG_PATH, 'r') as f:
                    cls._data = json.load(f)
                return

        cls._data = {
            "characters": characters,
            "items": items,
            "abilities": abilities,
            "perks": perks,
            "skins": skins,
            "progression": progression
        }

    @classmethod
    def _deduplicate(cls, data: List[Dict[str, Any]], model_class) -> List[Any]:
        seen = set()
        unique = []
        for item in data:
            guid = item.get("guid")
            if guid and guid not in seen:
                seen.add(guid)
                unique.append(model_class(**item))
        return unique

    @classmethod
    def get_characters(cls) -> List[CharacterData]:
        return cls._deduplicate(cls._data.get("characters", []), CharacterData)

    @classmethod
    def get_items(cls) -> List[ItemData]:
        return cls._deduplicate(cls._data.get("items", []), ItemData)

    @classmethod
    def get_abilities(cls) -> List[AbilityData]:
        return cls._deduplicate(cls._data.get("abilities", []), AbilityData)

    @classmethod
    def get_perks(cls) -> List[PerkData]:
        return cls._deduplicate(cls._data.get("perks", []), PerkData)

    @classmethod
    def get_skins(cls) -> List[SkinData]:
        return cls._deduplicate(cls._data.get("skins", []), SkinData)

    @classmethod
    def get_progression(cls) -> Dict[str, Any]:
         return cls._data.get("progression", {})

    @classmethod
    def get_all_item_guids(cls) -> List[str]:
        # Returns GUIDs of EVERYTHING (Items, Skins) for inventory population
        guids = []
        guids.extend([i.guid for i in cls.get_items()])
        guids.extend([s.guid for s in cls.get_skins()])
        # Abilities and Perks are usually attached to characters or levels, 
        # but sometimes unlocked globally. We can add them if needed.
        return guids

Catalog.load()
