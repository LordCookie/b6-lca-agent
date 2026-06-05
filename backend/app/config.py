from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False)

    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")

    app_log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")
    app_run_base_dir: Path = Field(default=Path("/app/runs"), alias="APP_RUN_BASE_DIR")

    oekobaudat_api_base: str = Field(
        default="https://oekobaudat.de/OEKOBAU.DAT/resource",
        alias="OEKOBAUDAT_API_BASE",
    )
    oekobaudat_db_version: str = Field(default="2024-I", alias="OEKOBAUDAT_DB_VERSION")

    timeout_osm_fetch: int = Field(default=900, alias="TIMEOUT_OSM_FETCH")
    timeout_deepness_infer: int = Field(default=1800, alias="TIMEOUT_DEEPNESS_INFER")
    timeout_oekobaudat: int = Field(default=300, alias="TIMEOUT_OEKOBAUDAT")
    timeout_llm_call: int = Field(default=180, alias="TIMEOUT_LLM_CALL")

    models_config_path: Path = Field(
        default=Path("/app/models.yaml"), alias="MODELS_CONFIG_PATH"
    )
    llm_pricing_path: Path = Field(
        default=Path("/app/data/llm/pricing.yaml"),
        alias="LLM_PRICING_PATH",
    )
    lookup_table_path: Path = Field(
        default=Path("/app/data/lookup/emission_factors.xlsx"),
        alias="LOOKUP_TABLE_PATH",
    )
    oekobaudat_csv_path: Path = Field(
        default=Path("/app/data/oekobaudat/oekobaudat_2024-I_2026-05-25.csv"),
        alias="OEKOBAUDAT_CSV_PATH",
    )
    carrier_map_path: Path = Field(
        default=Path("/app/data/oekobaudat/carrier_map.yaml"),
        alias="CARRIER_MAP_PATH",
    )
    deepness_onnx_path: Path = Field(
        default=Path("/app/data/deepness/tree_segmentation.onnx"),
        alias="DEEPNESS_ONNX_PATH",
    )

    climate_data_path: Path = Field(
        default=Path("/app/data/climate/DEU_NI_Oldenburg.102150_TMYx.2011-2025.epw"),
        alias="CLIMATE_DATA_PATH",
    )
    profiles_dir: Path = Field(
        default=Path("/app/data/profiles"),
        alias="PROFILES_DIR",
    )
    imagery_dir: Path = Field(
        default=Path("/app/data/imagery"),
        alias="IMAGERY_DIR",
    )
    deepness_tile_size: int = Field(default=512, alias="DEEPNESS_TILE_SIZE")
    deepness_tile_overlap: int = Field(default=64, alias="DEEPNESS_TILE_OVERLAP")
    deepness_threshold: float = Field(default=0.5, alias="DEEPNESS_THRESHOLD")
    # Multi-class segmentation: set to the class id that represents trees/canopy.
    # For DEEPNESS LandCover.ai model: class 2 = Woodland.
    # Leave None for binary single-class segmentation models.
    deepness_target_class: int | None = Field(default=None, alias="DEEPNESS_TARGET_CLASS")

    # Satellite-imagery fallback when no local DOP20 raster is available
    # for the AOI (keyless ESRI World Imagery XYZ tiles).
    tile_provider: str = Field(default="esri", alias="TILE_PROVIDER")
    tile_zoom: int = Field(default=18, alias="TILE_ZOOM")
    imagery_cache_dir: Path = Field(
        default=Path("/app/runs/.cache/satellite"),
        alias="IMAGERY_CACHE_DIR",
    )
    # OSM vegetation tags (natural=tree, landuse=forest, leisure=park, …) merged
    # with the ONNX canopy segmentation for cross-validation.
    osm_vegetation_enabled: bool = Field(default=True, alias="OSM_VEGETATION_ENABLED")
    # Default crown diameter (m) for OSM-tree nodes the ONNX missed — used to
    # compute their canopy-area contribution.
    osm_tree_default_crown_m: float = Field(default=5.0, alias="OSM_TREE_DEFAULT_CROWN_M")

    # Spec §13 — LLM-Recherche im Research-Agent aktivieren. Spec §10 bleibt
    # gewahrt, weil der LLM-Call das openrouter:web_search Tool nutzt und
    # nur Werte mit zitierter URL übernommen werden.
    research_llm_fallback: bool = Field(default=True, alias="RESEARCH_LLM_FALLBACK")
    # Cost-Cap: maximale Anzahl LLM-Recherche-Calls pro Lauf.
    research_llm_max_calls_per_run: int = Field(
        default=10, alias="RESEARCH_LLM_MAX_CALLS_PER_RUN"
    )
    # Eigene Web-Suche (llm/web_search.py, schlüsselloses DuckDuckGo) statt
    # OpenRouters Server-Tool. Liefert echte zitierbare URLs (Spec §10).
    web_search_provider: str = Field(default="duckduckgo", alias="WEB_SEARCH_PROVIDER")
    web_search_max_results: int = Field(default=5, alias="WEB_SEARCH_MAX_RESULTS")
    web_search_region: str = Field(default="de-de", alias="WEB_SEARCH_REGION")
    # Vegetations-Agent recherchiert die dominante Baumgruppe für die Allometrie.
    vegetation_species_research: bool = Field(
        default=True, alias="VEGETATION_SPECIES_RESEARCH"
    )
    # KI-Empfehlungen im Reporting-Agent (Spec §1+§13)
    reporting_llm_enabled: bool = Field(default=True, alias="REPORTING_LLM_ENABLED")
    reporting_llm_use_websearch: bool = Field(
        default=True, alias="REPORTING_LLM_USE_WEBSEARCH"
    )
    # Outlier-Kommentare via Research-Agent LLM
    validation_llm_enabled: bool = Field(default=True, alias="VALIDATION_LLM_ENABLED")
    # UUID-Vorschläge im Material-Agent
    material_llm_enabled: bool = Field(default=True, alias="MATERIAL_LLM_ENABLED")

    # Spec §5: Bezugsjahr für die Bilanzierung — landet im manifest.json
    # und im Report-Header. Beeinflusst aktuell keine Berechnung direkt
    # (ÖKOBAUDAT-Snapshot und Klimajahr werden separat versioniert),
    # ist aber für die Reproduzierbarkeit ausdrücklich zu dokumentieren.
    reference_year: int = Field(default=2026, alias="REFERENCE_YEAR")


settings = Settings()
