import os.path
import re
from typing import Optional

from pystac import (
    CatalogType,
    Collection,
    Extent,
    Asset,
    Summaries,
    SpatialExtent,
    TemporalExtent,
)
from pystac.extensions.grid import GridExtension
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.item_assets import ItemAssetsExtension
from pystac.extensions.raster import (
    DataType,
    RasterBand,
    RasterExtension,
    Sampling,
)
from pystac.media_type import MediaType

import rasterio
from shapely.geometry import mapping, box, shape as make_shape
from pystac import Item

from stactools.core.io import ReadHrefModifier
from stactools.core.utils import href_exists
from stactools.cop_dem import constants as co


def create_item(
    href: str,
    read_href_modifier: Optional[ReadHrefModifier] = None,
    host: Optional[str] = None,
) -> Item:
    """Creates a STAC Item from a single tile of Copernicus DEM data."""
    if read_href_modifier:
        modified_href = read_href_modifier(href)
    else:
        modified_href = href
    with rasterio.open(modified_href) as dataset:
        if dataset.crs.to_epsg() != co.COP_DEM_EPSG:
            raise ValueError(f"Dataset {href} is not EPSG:{co.COP_DEM_EPSG}, "
                             "which is required for Copernicus DEM data")
        bbox = list(dataset.bounds)
        geometry = mapping(box(*bbox))
        transform = dataset.transform
        shape = dataset.shape
        item = Item(
            id=os.path.splitext(os.path.basename(href))[0],
            geometry=geometry,
            bbox=bbox,
            datetime=co.COP_DEM_COLLECTION_START,
            properties={},
            stac_extensions=[],
        )

    # resolution in arc seconds (not meters!), which is and 30 for GLO-90 and 10 for GLO-30
    p = re.compile(
        r"Copernicus_DSM_COG_(?P<res>\d{2})_(?P<northing>[NS]\d{2})_00_(?P<easting>[EW]\d{3})_00_DEM.*"  # noqa: E501
    )
    if m := p.match(os.path.basename(href)):
        res = m.group("res")
        if res == "30":
            gsd = 90
        elif res == "10":
            gsd = 30
        else:
            raise ValueError(f"unknown resolution {res}")

        northing = m.group("northing")
        easting = m.group("easting")
    else:
        raise ValueError(f"unable to parse {href}")

    item.add_links(co.COP_DEM_LINKS)
    item.common_metadata.platform = co.COP_DEM_PLATFORM
    item.common_metadata.gsd = gsd

    if host and host not in co.COP_DEM_HOST.keys():
        raise ValueError(
            f"Invalid host: {host}. Must be one of {list(co.COP_DEM_HOST.keys())}"
        )
    if host and (host_provider := co.COP_DEM_HOST.get(host)):
        providers = [*co.COP_DEM_PROVIDERS, host_provider]
    else:
        providers = co.COP_DEM_PROVIDERS

    item.common_metadata.providers = providers
    item.common_metadata.license = "proprietary"

    data_asset = Asset(
        href=href,
        title="Data",
        description=None,
        media_type=MediaType.COG,
        roles=["data"],
    )
    data_bands = RasterBand.create(
        sampling=Sampling.POINT,
        data_type=DataType.FLOAT32,
        spatial_resolution=gsd,
        unit="meter",
    )

    item.add_asset("data", data_asset)
    RasterExtension.ext(data_asset, add_if_missing=True).bands = [data_bands]

    # add additional assets
    full_asset_switch_tbd = True
    if full_asset_switch_tbd:
        home = os.path.dirname(href)
        cog_tile = os.path.basename(home).replace("_DEM", "")
        tile = cog_tile.replace("_COG", "")

        meta_assets = {
            "metadata": {
                "href": os.path.join(home, f"{tile}.xml"),
                "title": "Metadata",
                "description": "ISO 19115 compliant item metadata in xml format",
                "type": MediaType.XML,
                "roles": ["metadata", "accuracy-mask"],
            },
            "source_mask": {
                "href": os.path.join(home, "PREVIEW", f"{tile}_SRC.kml"),
                "title": "Source Scenes (SRC)",
                "description": "Footprints of source scenes used to derive the DEM layer, xml vector format",
                "type": "application/vnd.google-earth.kml+xml",
                "roles": ["metadata", "source-mask"],
            },
            "editing_mask": {
                "href": os.path.join(home, "AUXFILES", f"{cog_tile}_EDM.tif"),
                "title": "Editing Mask (EDM)",
                "description": "Mask indicating whether a pixel was edited (see specification for more details)",
                "type": MediaType.GEOTIFF,
                "roles": ["metadata", "editing-mask"],
            },
            "filling_mask": {
                "href": os.path.join(home, "AUXFILES", f"{cog_tile}_FLM.tif"),
                "title": "Filling Mask (FLM)",
                "description": "Mask indicating whether a pixel was filled from external sources including fill source (see specification for more details)",
                "type": MediaType.GEOTIFF,
                "roles": ["metadata", "filling-mask"],
            },
            "water_body_mask": {
                "href": os.path.join(home, "AUXFILES", f"{cog_tile}_WBM.tif"),
                "title": "Water Body Mask (WBM)",
                "description": "Mask indicating whether a pixel is a modified water pixel (see specification for more details)",
                "type": MediaType.GEOTIFF,
                "roles": ["metadata", "water-mask"],
            },
            "height_error_mask": {
                "href": os.path.join(home, "AUXFILES", f"{cog_tile}_HEM.tif"),
                "title": "Height Error Mask (HEM)",
                "description": "Mask indicating height error as standard deviation for each DEM pixel (see specification for more details)",
                "type": MediaType.GEOTIFF,
                "roles": ["metadata", "error-mask"],
            },
            "vertical_accuracy_mask": {
                "href": os.path.join(home, "AUXFILES", f"{tile}_ACM.kml"),
                "title": "Vertical Accuracy Mask (ACM)",
                "description": "The Accuracy Layer provides the absolute, vertical accuracy information expressed in the estimated mean (68%) and maximum (90%) vertical accuracy per delivery unit as a vector file (KML format)",
                "type": "application/vnd.google-earth.kml+xml",
                "roles": ["metadata", "accuracy-mask"],
            },
        }

        for meta_asset_key, meta_asset in meta_assets.items():
            if meta_asset["href"].startswith("http"):
                meta_asset["href"] = meta_asset["href"].replace(os.sep, "/") # Windows OS backslash handling
            if href_exists(meta_asset["href"]):
                item.add_asset(meta_asset_key, Asset(
                    href=meta_asset["href"],
                    title=meta_asset["title"],
                    description=meta_asset["description"],
                    media_type=meta_asset["type"],
                    roles=meta_asset["roles"]
                ))

    projection = ProjectionExtension.ext(item, add_if_missing=True)
    projection.epsg = co.COP_DEM_EPSG
    projection.transform = transform[0:6]
    projection.shape = shape

    centroid = make_shape(item.geometry).centroid
    projection.centroid = {
        "lat": round(centroid.y, 1),
        "lon": round(centroid.x, 1)
    }

    grid = GridExtension.ext(item, add_if_missing=True)
    grid.code = f"CDEM-{northing}{easting}"

    return item


def create_collection(product: str, host: Optional[str] = None) -> Collection:
    """Create a STAC Collection

    This function includes logic to extract all relevant metadata from
    an asset describing the STAC collection and/or metadata coded into an
    accompanying constants.py file.

    See `Collection<https://pystac.readthedocs.io/en/latest/api.html#collection>`_.

    Args:
        Product (str): glo-30 or glo-90

    Returns:
        Item: STAC Item object

    Returns:
        Collection: STAC Collection object
    """
    if product.lower() == "glo-30":
        summaries = {
            "gsd": [30],
            "platform": [co.COP_DEM_PLATFORM],
            # "instruments": ,
        }
    elif product.lower() == "glo-90":
        summaries = {
            "gsd": [90],
            "platform": [co.COP_DEM_PLATFORM],
            # "instruments": ,
        }
    else:
        raise ValueError(
            f"{product} is not a valid product. Must be one of {co.COP_DEM_PRODUCTS}"
        )

    # Allow host to be selected by cli option
    if host and host not in co.COP_DEM_HOST.keys():
        raise ValueError(
            f"Invalid host: {host}. Must be one of {list(co.COP_DEM_HOST.keys())}"
        )
    if host and (host_provider := co.COP_DEM_HOST.get(host)):
        providers = [*co.COP_DEM_PROVIDERS, host_provider]
    else:
        providers = co.COP_DEM_PROVIDERS

    collection = Collection(
        id=f"cop-dem-{product.lower()}",
        title=f"Copernicus DEM {product.upper()}",
        description=co.COP_DEM_DESCRIPTION,
        license="proprietary",
        keywords=co.COP_DEM_KEYWORDS,
        catalog_type=CatalogType.RELATIVE_PUBLISHED,
        summaries=Summaries(summaries),
        extent=Extent(
            SpatialExtent(co.COP_DEM_SPATIAL_EXTENT),
            TemporalExtent([co.COP_DEM_TEMPORAL_EXTENT]),
        ),
        providers=providers,
        stac_extensions=[
            ItemAssetsExtension.get_schema_uri(),
            ProjectionExtension.get_schema_uri(),
            RasterExtension.get_schema_uri(),
        ],
    )

    collection.add_links(co.COP_DEM_LINKS)

    assets = ItemAssetsExtension.ext(collection, add_if_missing=True)
    assets.item_assets = co.COP_DEM_ASSETS

    return collection
