from __future__ import annotations

from collections import defaultdict

from .matching import normalize_title
from .models import Film, LanguageTier, ReconciliationStatus
from .providers.prehrajto import ProviderError
from .ranking import rank_candidates
from .reconciliation import reconcile_film
from .state import StateStore
from .transfer import TransferError

MAX_PILOT_FILMS = 10
MAX_PRODUCTION_BATCH = 20


def validate_limit(limit: int, maximum: int = MAX_PILOT_FILMS) -> int:
    if not 1 <= limit <= maximum:
        raise ValueError(f"Limit must be between 1 and {maximum}")
    return limit


class HybridPipeline:
    def __init__(
        self,
        *,
        prehrajto,
        sktorrent,
        inventory,
        state: StateStore,
        transfer=None,
        historical=None,
        defer_processing_verification: bool = False,
    ):
        self.prehrajto = prehrajto
        self.sktorrent = sktorrent
        self.inventory = inventory
        self.state = state
        self.transfer = transfer
        self.historical = historical or {}
        self.defer_processing_verification = defer_processing_verification
        self._candidates: dict[int, list] = {}
        self._subtitle_repairs: dict[int, str] = {}
        self._inventory_by_id = {str(item.video_id): item for item in inventory}
        self._inventory_by_token = defaultdict(list)
        for item in inventory:
            for token in set(normalize_title(item.name).split()):
                self._inventory_by_token[token].append(item)

    def _plausible_inventory(self, film: Film):
        plausible = {}
        for alias in (film.title, film.original_title):
            tokens = normalize_title(alias or "").split()
            if not tokens:
                continue
            rarest = min(tokens, key=lambda token: len(self._inventory_by_token[token]))
            for item in self._inventory_by_token[rarest]:
                plausible[item.video_id] = item
        historical_id = self.historical.get(film.cr_film_id)
        if historical_id and str(historical_id) in self._inventory_by_id:
            item = self._inventory_by_id[str(historical_id)]
            plausible[item.video_id] = item
        return list(plausible.values())

    def build_plan(
        self,
        films: list[Film],
        limit: int,
        *,
        maximum: int = MAX_PILOT_FILMS,
        skip_exhausted_snapshot: bool = False,
    ) -> list[dict]:
        validate_limit(limit, maximum)
        plan = []
        classifications_since_save = 0
        for film in sorted(
            films,
            key=lambda item: (item.added_at or item.created_at or "", item.cr_film_id),
            reverse=True,
        ):
            if self.state.uploaded(film.cr_film_id):
                continue
            if skip_exhausted_snapshot and self.state.discovery_exhausted_for_snapshot(
                film.cr_film_id
            ):
                continue
            partial = self.state.pending_partial_upload(film.cr_film_id)
            partial_id = (
                str(partial.get("partial_target_video_id")) if partial else None
            )
            if partial_id and partial_id in self._inventory_by_id:
                status = ReconciliationStatus.MISSING
                evidence = {
                    "method": "partial_upload_requires_subtitle_repair",
                    "target_video_id": partial_id,
                }
                self._subtitle_repairs[film.cr_film_id] = partial_id
            else:
                status, evidence = reconcile_film(
                    film,
                    self._plausible_inventory(film),
                    self.historical.get(film.cr_film_id),
                )
            self.state.record_reconciliation(
                film.cr_film_id,
                status.value,
                evidence,
                {
                    "slug": film.slug,
                    "title": film.title,
                    "original_title": film.original_title,
                    "year": film.year,
                    "runtime_min": film.runtime_min,
                    "tmdb_id": film.tmdb_id,
                    "imdb_id": film.imdb_id,
                    "created_at": film.created_at,
                    "added_at": film.added_at,
                },
                persist=status == ReconciliationStatus.MISSING,
            )
            if status != ReconciliationStatus.MISSING:
                classifications_since_save += 1
                if classifications_since_save >= 100:
                    self.state.save()
                    classifications_since_save = 0
                continue
            repair_provider = partial.get("provider") if partial else None
            provider_errors = []
            try:
                prehraj_candidates = (
                    self.prehrajto.discover(film)
                    if repair_provider in (None, "prehrajto")
                    else []
                )
            except ProviderError as error:
                prehraj_candidates = []
                provider_errors.append({"provider": "prehrajto", "reason": str(error)})
            ranked = rank_candidates(prehraj_candidates)
            all_candidates = list(prehraj_candidates)
            if not ranked and repair_provider in (None, "sktorrent"):
                sk_candidates = self.sktorrent.discover(film)
                all_candidates.extend(sk_candidates)
                ranked = rank_candidates(sk_candidates)
            if film.cr_film_id in self._subtitle_repairs and partial:
                ranked = [
                    candidate
                    for candidate in ranked
                    if candidate.provider == partial.get("provider")
                    and candidate.source_id == str(partial.get("source_id"))
                    and candidate.language_tier == LanguageTier.CZECH_SUBTITLES
                ]
            burned = self.state.burned_source_ids(film.cr_film_id)
            ranked = [
                candidate for candidate in ranked if candidate.source_id not in burned
            ]
            self._candidates[film.cr_film_id] = ranked
            selected = ranked[0] if ranked else None
            if selected is None:
                self.state.record_attempt(
                    film.cr_film_id,
                    {
                        "status": "no_acceptable_source",
                        "permanent": False,
                        "discovery_exhausted": True,
                        "reason": "No currently acceptable source was discovered",
                        "candidate_evidence": [
                            candidate.to_dict() for candidate in all_candidates
                        ],
                    },
                )
                continue
            plan.append(
                {
                    "film": film.to_dict(),
                    "reconciliation": {"status": status.value, "evidence": evidence},
                    "selected": selected.to_dict(),
                    "subtitle_handling": (
                        "external Czech track will be uploaded and verified"
                        if selected
                        and selected.language_tier == LanguageTier.CZECH_SUBTITLES
                        else "not required"
                    ),
                    "candidates": [candidate.to_dict() for candidate in all_candidates],
                    "provider_errors": provider_errors,
                }
            )
            if len(plan) >= limit:
                break
        if classifications_since_save:
            self.state.save()
        return plan

    def execute(self, plan: list[dict]) -> None:
        if self.transfer is None:
            raise RuntimeError("Transfer service is required for upload mode")
        for row in plan:
            film = Film.from_dict(row["film"])
            if self.state.uploaded(film.cr_film_id):
                continue
            candidates = list(self._candidates.get(film.cr_film_id, []))
            # A failed Prehraj.to transfer may fall through to SK Torrent.
            if film.cr_film_id in self._subtitle_repairs:
                fallback_loaded = True
            elif candidates and all(
                candidate.provider == "prehrajto" for candidate in candidates
            ):
                fallback_loaded = False
            else:
                fallback_loaded = True
            success = False
            while True:
                for candidate in candidates:
                    try:
                        if film.cr_film_id in self._subtitle_repairs:
                            upload = self.transfer.repair_subtitle(
                                film,
                                candidate,
                                self._subtitle_repairs[film.cr_film_id],
                            )
                        else:
                            upload = self.transfer.transfer(film, candidate)
                    except TransferError as error:
                        self.state.record_attempt(
                            film.cr_film_id,
                            {
                                "provider": candidate.provider,
                                "source_id": candidate.source_id,
                                "status": "failed",
                                "reason": str(error),
                                "permanent": error.permanent,
                                "partial_target_video_id": error.target_video_id,
                            },
                        )
                        continue
                    self.state.record_success(
                        film.cr_film_id,
                        {
                            **upload,
                            "provider": candidate.provider,
                            "source_id": candidate.source_id,
                            "language_tier": candidate.language_tier.name.lower(),
                            "actual_resolution": candidate.resolution,
                            "subtitle_handling": row["subtitle_handling"],
                            "processing_status": (
                                "pending"
                                if self.defer_processing_verification
                                else "active"
                            ),
                        },
                    )
                    success = True
                    break
                if success or fallback_loaded:
                    break
                fallback_loaded = True
                candidates = rank_candidates(self.sktorrent.discover(film))
            if not success:
                self.state.record_attempt(
                    film.cr_film_id,
                    {
                        "status": "no_acceptable_source",
                        "permanent": False,
                        "discovery_exhausted": False,
                        "reason": "All currently acceptable candidates were exhausted",
                    },
                )
