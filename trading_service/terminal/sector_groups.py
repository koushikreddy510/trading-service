"""NSE industry -> parent sector map, copied from strategy-screener/sector_groups.py so the terminal does not import
another project. Keep the two in step: ~185 granular `symbols.sector` values roll up into ~25 groups."""
SECTOR_GROUP_MAP = {
    # Automobiles & Auto Components
    "2/3 Wheelers": "Automobiles",
    "Auto Components & Equipments": "Automobiles",
    "Auto Dealer": "Automobiles",
    "Commercial Vehicles": "Automobiles",
    "Construction Vehicles": "Automobiles",
    "Cycles": "Automobiles",
    "Passenger Cars & Utility Vehicles": "Automobiles",
    "Tractors": "Automobiles",
    "Trading - Auto Components": "Automobiles",
    "Tyres & Rubber Products": "Automobiles",

    # Banking & Finance
    "Private Sector Bank": "Banking & Finance",
    "Public Sector Bank": "Banking & Finance",
    "Other Bank": "Banking & Finance",
    "Non Banking Financial Company (NBFC)": "Banking & Finance",
    "Housing Finance Company": "Banking & Finance",
    "Microfinance Institutions": "Banking & Finance",
    "Financial Institution": "Banking & Finance",
    "Financial Products Distributor": "Banking & Finance",
    "Financial Technology (Fintech)": "Banking & Finance",
    "Other Financial Services": "Banking & Finance",
    "Depositories Clearing Houses and Other Intermediaries": "Banking & Finance",
    "Stockbroking & Allied": "Banking & Finance",
    "Other Capital Market related Services": "Banking & Finance",
    "Exchange and Data Platform": "Banking & Finance",
    "Ratings": "Banking & Finance",

    # Insurance
    "General Insurance": "Insurance",
    "Life Insurance": "Insurance",
    "Insurance Distributors": "Insurance",

    # Asset Management & Investment
    "Asset Management Company": "Asset Management",
    "Investment Company": "Asset Management",
    "Mutual Fund Scheme": "Asset Management",
    "MUTUAL FUND SCHEME": "Asset Management",

    # IT & Technology
    "Computers - Software & Consulting": "IT & Technology",
    "Computers Hardware & Equipments": "IT & Technology",
    "IT Enabled Services": "IT & Technology",
    "Software Products": "IT & Technology",
    "Data Processing Services": "IT & Technology",
    "Business Process Outsourcing (BPO)/ Knowledge Process Outsourcing (KPO)": "IT & Technology",
    "E-Learning": "IT & Technology",

    # Telecom
    "Telecom - Cellular & Fixed line services": "Telecom",
    "Telecom - Equipment & Accessories": "Telecom",
    "Telecom - Infrastructure": "Telecom",
    "Other Telecom Services": "Telecom",

    # Media & Entertainment
    "Advertising & Media Agencies": "Media & Entertainment",
    "Digital Entertainment": "Media & Entertainment",
    "Electronic Media": "Media & Entertainment",
    "Film Production Distribution & Exhibition": "Media & Entertainment",
    "Media & Entertainment": "Media & Entertainment",
    "Print Media": "Media & Entertainment",
    "Printing & Publication": "Media & Entertainment",
    "TV Broadcasting & Software Production": "Media & Entertainment",
    "Web based media and service": "Media & Entertainment",

    # Pharma & Healthcare
    "Pharmaceuticals": "Pharma & Healthcare",
    "Biotechnology": "Pharma & Healthcare",
    "Healthcare Research Analytics & Technology": "Pharma & Healthcare",
    "Healthcare Service Provider": "Pharma & Healthcare",
    "Hospital": "Pharma & Healthcare",
    "Medical Equipment & Supplies": "Pharma & Healthcare",
    "Pharmacy Retail": "Pharma & Healthcare",
    "Wellness": "Pharma & Healthcare",

    # FMCG & Consumer
    "Diversified FMCG": "FMCG & Consumer",
    "Household Products": "FMCG & Consumer",
    "Household Appliances": "FMCG & Consumer",
    "Houseware": "FMCG & Consumer",
    "Personal Care": "FMCG & Consumer",
    "Consumer Electronics": "FMCG & Consumer",
    "Footwear": "FMCG & Consumer",
    "Garments & Apparels": "FMCG & Consumer",
    "Gems Jewellery And Watches": "FMCG & Consumer",
    "Furniture Home Furnishing": "FMCG & Consumer",
    "Leisure Products": "FMCG & Consumer",
    "Sanitary Ware": "FMCG & Consumer",
    "Stationary": "FMCG & Consumer",
    "Cigarettes & Tobacco Products": "FMCG & Consumer",

    # Food & Beverages
    "Breweries & Distilleries": "Food & Beverages",
    "Dairy Products": "Food & Beverages",
    "Edible Oil": "Food & Beverages",
    "Other Beverages": "Food & Beverages",
    "Other Food Products": "Food & Beverages",
    "Packaged Foods": "Food & Beverages",
    "Sugar": "Food & Beverages",
    "Tea & Coffee": "Food & Beverages",
    "Animal Feed": "Food & Beverages",
    "Meat Products including Poultry": "Food & Beverages",
    "Seafood": "Food & Beverages",

    # Chemicals
    "Commodity Chemicals": "Chemicals",
    "Specialty Chemicals": "Chemicals",
    "Dyes And Pigments": "Chemicals",
    "Explosives": "Chemicals",
    "Fertilizers": "Chemicals",
    "Pesticides & Agrochemicals": "Chemicals",
    "Petrochemicals": "Chemicals",
    "Trading - Chemicals": "Chemicals",
    "Printing Inks": "Chemicals",
    "Paints": "Chemicals",

    # Metals & Mining
    "Aluminium": "Metals & Mining",
    "Aluminium Copper & Zinc Products": "Metals & Mining",
    "Copper": "Metals & Mining",
    "Diversified Metals": "Metals & Mining",
    "Ferro & Silica Manganese": "Metals & Mining",
    "Iron & Steel": "Metals & Mining",
    "Iron & Steel Products": "Metals & Mining",
    "Sponge Iron": "Metals & Mining",
    "Zinc": "Metals & Mining",
    "Industrial Minerals": "Metals & Mining",
    "Trading - Metals": "Metals & Mining",
    "Trading - Minerals": "Metals & Mining",
    "Carbon Black": "Metals & Mining",
    "Coal": "Metals & Mining",
    "Trading - Coal": "Metals & Mining",

    # Oil & Gas
    "Gas Transmission/Marketing": "Oil & Gas",
    "LPG/CNG/PNG/LNG Supplier": "Oil & Gas",
    "Oil Equipment & Services": "Oil & Gas",
    "Oil Exploration & Production": "Oil & Gas",
    "Oil Storage & Transportation": "Oil & Gas",
    "Refineries & Marketing": "Oil & Gas",
    "Trading - Gas": "Oil & Gas",
    "Lubricants": "Oil & Gas",

    # Power & Energy
    "Integrated Power Utilities": "Power & Energy",
    "Power Distribution": "Power & Energy",
    "Power Generation": "Power & Energy",
    "Power Trading": "Power & Energy",
    "Power - Transmission": "Power & Energy",

    # Construction & Real Estate
    "Civil Construction": "Construction & Infra",
    "Other Construction Materials": "Construction & Infra",
    "Cement & Cement Products": "Construction & Infra",
    "Residential Commercial Projects": "Construction & Infra",
    "Real Estate related services": "Construction & Infra",
    "Granites & Marbles": "Construction & Infra",
    "Glass - Consumer": "Construction & Infra",
    "Glass - Industrial": "Construction & Infra",
    "Ceramics": "Construction & Infra",
    "Plywood Boards/ Laminates": "Construction & Infra",

    # Capital Goods & Engineering
    "Abrasives & Bearings": "Capital Goods",
    "Castings & Forgings": "Capital Goods",
    "Compressors Pumps & Diesel Engines": "Capital Goods",
    "Heavy Electrical Equipment": "Capital Goods",
    "Industrial Products": "Capital Goods",
    "Other Industrial Products": "Capital Goods",
    "Other Electrical Equipment": "Capital Goods",
    "Cables - Electricals": "Capital Goods",
    "Electrodes & Refractories": "Capital Goods",
    "Industrial Gases": "Capital Goods",
    "Railway Wagons": "Capital Goods",

    # Defence & Aerospace
    "Aerospace & Defense": "Defence & Aerospace",
    "Ship Building & Allied Services": "Defence & Aerospace",

    # Transport & Logistics
    "Airline": "Transport & Logistics",
    "Airport & Airport services": "Transport & Logistics",
    "Logistics Solution Provider": "Transport & Logistics",
    "Port & Port services": "Transport & Logistics",
    "Road Assets - Toll Annuity Hybrid-Annuity": "Transport & Logistics",
    "Road Transport": "Transport & Logistics",
    "Shipping": "Transport & Logistics",
    "Transport Related Services": "Transport & Logistics",
    "Dredging": "Transport & Logistics",
    "Offshore Support Solution Drilling": "Transport & Logistics",

    # Textiles
    "Other Textile Products": "Textiles",
    "Jute & Jute Products": "Textiles",
    "Leather And Leather Products": "Textiles",
    "Trading - Textile Products": "Textiles",

    # Retail & E-Commerce
    "Diversified Retail": "Retail & E-Commerce",
    "E-Retail/ E-Commerce": "Retail & E-Commerce",
    "Internet & Catalogue Retail": "Retail & E-Commerce",
    "Speciality Retail": "Retail & E-Commerce",

    # Paper & Packaging
    "Paper & Paper Products": "Paper & Packaging",
    "Packaging": "Paper & Packaging",

    # Plastics & Rubber
    "Plastic Products - Consumer": "Plastics & Rubber",
    "Plastic Products - Industrial": "Plastics & Rubber",
    "Rubber": "Plastics & Rubber",

    # Agri & Allied
    "Other Agricultural Products": "Agriculture",

    # Services & Others
    "Consulting Services": "Services & Others",
    "Diversified": "Services & Others",
    "Diversified Commercial Services": "Services & Others",
    "Education": "Services & Others",
    "Hotels & Resorts": "Services & Others",
    "Restaurants": "Services & Others",
    "Tour Travel Related Services": "Services & Others",
    "Amusement Parks/ Other Recreation": "Services & Others",
    "Other Consumer Services": "Services & Others",
    "Holding Company": "Services & Others",
    "Waste Management": "Services & Others",
    "Water Supply & Management": "Services & Others",
    "Trading & Distributors": "Services & Others",
    "NA": "Services & Others",

    # Dealers (catch-all for garbled entries)
    "Dealers\u2013\u2013\u2013Commercial Vehicles Tractors Construction Vehicles": "Automobiles",
}


# Yahoo Finance sector names (used when symbols.sector is populated via yfinance).
# These map each Yahoo sector to itself so the view shows them as top-level groups.
_YAHOO_SECTORS = [
    "Financial Services", "Technology", "Healthcare", "Consumer Cyclical",
    "Consumer Defensive", "Energy", "Basic Materials", "Industrials",
    "Utilities", "Real Estate", "Communication Services",
]
for _s in _YAHOO_SECTORS:
    if _s not in SECTOR_GROUP_MAP:
        SECTOR_GROUP_MAP[_s] = _s


def get_parent_sector(sector: str) -> str:
    """Return the parent group for a given NSE sector or Yahoo Finance sector, or 'Others' if unmapped."""
    return SECTOR_GROUP_MAP.get(sector, "Others")


# NSE sector indices: parent sector -> index symbol (for display/reference)
# Note: Fyers NSE_CM is equity-only; these indices are for context. Add to symbols when index master is available.
SECTOR_INDICES = {
    "Automobiles": "NIFTY AUTO",
    "Banking & Finance": "NIFTY BANK",
    "IT & Technology": "NIFTY IT",
    "Pharma & Healthcare": "NIFTY PHARMA",
    "FMCG & Consumer": "NIFTY FMCG",
    "Metals & Mining": "NIFTY METAL",
    "Oil & Gas": "NIFTY ENERGY",
    "Power & Energy": "NIFTY ENERGY",
    "Realty": "NIFTY REALTY",
}
# Alias for Metals & Mining (NSE uses "NIFTY METAL")
SECTOR_INDICES["Metals & Mining"] = "NIFTY METAL"

# Ordered list of parent sectors for consistent UI display
PARENT_SECTORS = [
    "Automobiles",
    "Banking & Finance",
    "Insurance",
    "Asset Management",
    "IT & Technology",
    "Telecom",
    "Media & Entertainment",
    "Pharma & Healthcare",
    "FMCG & Consumer",
    "Food & Beverages",
    "Chemicals",
    "Metals & Mining",
    "Oil & Gas",
    "Power & Energy",
    "Construction & Infra",
    "Capital Goods",
    "Defence & Aerospace",
    "Transport & Logistics",
    "Textiles",
    "Retail & E-Commerce",
    "Paper & Packaging",
    "Plastics & Rubber",
    "Agriculture",
    "Services & Others",
]
