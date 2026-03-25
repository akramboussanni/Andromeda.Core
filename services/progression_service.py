import random
from typing import List, Optional, Set, Tuple

from catalog import Catalog
from models import PlayerCharacterLevelData


class ProgressionService:
    MAX_LEVEL_CAP = 20

    @staticmethod
    def _get_perk_tier_for_ascension(ascension: int) -> int:
        """
        Determine perk tier based on ascension with weighted probabilities.
        """
        if ascension <= 1:
            return 0
        elif ascension == 2:
            return 1 if random.random() < 0.9 else 2
        elif ascension == 3:
            return 1 if random.random() < 0.7 else 2
        elif ascension == 4:
            return 1 if random.random() < 0.5 else 2
        elif ascension == 5:
            return 1 if random.random() < 0.2 else 2
        else:
            return 2

    @staticmethod
    def _is_perk_unlocked(guid: str, tiers: List[str], unlocked_set: Set[str]) -> bool:
        if guid in unlocked_set: return True
        for t in tiers:
            if t in unlocked_set: return True
        return False

    @classmethod
    def get_level_offers(cls, char_guid: str, level_to_unlock: int, ascension: int, 
                       unlocked_abilities: Set[str], unlocked_perks: Set[str]) -> Tuple[List[str], List[str]]:
        """
        Calculate offers for the level we are ABOUT to unlock.
        """
        progression = Catalog.get_progression()
        char_prog = progression.get("characters", {}).get(char_guid)
        if not char_prog:
            return ([], [])
        
        perk_tier = cls._get_perk_tier_for_ascension(ascension)
        char_perks = char_prog.get("perks", [])
        general_perks = progression.get("general_perks", [])
        
        offered_perks = []
        offered_abilities = [] # Abilities usually handled specially or via specific logic
        
        # Logic for perk selection based on level
        if level_to_unlock == 7:
            # CHAR PERKS
             for p_entry in char_perks:
                p_tiers = p_entry.get("tiers", [])
                if p_tiers:
                    t_idx = min(perk_tier, len(p_tiers) - 1)
                    guid = p_tiers[t_idx]
                    if not cls._is_perk_unlocked(guid, p_tiers, unlocked_perks):
                         offered_perks.append(guid)
        
        elif level_to_unlock == 12:
             for p_entry in char_perks:
                p_tiers = p_entry.get("tiers", [])
                if p_tiers:
                    t_idx = min(perk_tier, len(p_tiers) - 1)
                    guid = p_tiers[t_idx]
                    if not cls._is_perk_unlocked(guid, p_tiers, unlocked_perks):
                         offered_perks.append(guid)
                         
             if len(offered_perks) > 1:
                 offered_perks = random.sample(offered_perks, min(2, len(offered_perks)))
                 
        else:
             # GENERAL PERKS
             available = []
             for p_entry in general_perks:
                p_tiers = p_entry.get("tiers", [])
                if p_tiers:
                    t_idx = min(perk_tier, len(p_tiers) - 1)
                    guid = p_tiers[t_idx]
                    if not cls._is_perk_unlocked(guid, p_tiers, unlocked_perks):
                        available.append(guid)
             
             if available:
                 count = min(2, len(available))
                 offered_perks = random.sample(available, count)

        return (offered_perks, offered_abilities)

    @classmethod
    def generate_level_1_logic(cls, char_guid: str, ascension: int) -> Tuple[Optional[str], List[PlayerCharacterLevelData]]:
        """
        Generates the Level 1 history entry (Ability Unlock) and returns (ability_guid, [history_entry]).
        """
        progression = Catalog.get_progression()
        char_prog = progression.get("characters", {}).get(char_guid)
        
        # Determine starting ability
        target_ability = None
        if char_prog:
            ability_info = char_prog.get("ability")
            if isinstance(ability_info, dict):
                ab_tiers = ability_info.get("tiers", [])
                if ab_tiers:
                    idx = min(ascension, len(ab_tiers) - 1)
                    target_ability = ab_tiers[idx]
        
        if not target_ability:
             return None, []

        pass_simulated_perks = []
        gen_perks_data = progression.get("general_perks", [])
        
        # Flatten tiers to just get valid Tier 0/1 guids for display "what could have been"
        safe_perks = []
        for p in gen_perks_data:
            tiers = p.get("tiers", [])
            if tiers: 
                safe_perks.append(tiers[0])
        
        if safe_perks:
            count = min(3, len(safe_perks))
            pass_simulated_perks = random.sample(safe_perks, count)
            
        history_entry = PlayerCharacterLevelData(
            offeredAbilities=[target_ability],
            offeredPerks=pass_simulated_perks,
            chosenAbility=target_ability,
            chosenPerk=None
        )
        
        return target_ability, [history_entry]
