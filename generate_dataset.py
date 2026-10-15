import pandas as pd
import random
import os

random.seed(42)

# ==========================================
# CITY + AREA + LOCALITY PRICE LEVEL
# ==========================================

locations = {
    "Ahmedabad": {
        "Bodakdev": 1.35,
        "Thaltej": 1.30,
        "Satellite": 1.28,
        "Vastrapur": 1.25,
        "Prahlad Nagar": 1.22,
        "Science City": 1.20,
        "Ambli": 1.30,
        "Bopal": 1.05,
        "South Bopal": 1.08,
        "Gota": 0.95,
        "Chandkheda": 0.90,
        "Nikol": 0.88,
        "Vastral": 0.85,
        "Maninagar": 1.00,
        "Motera": 1.12,
        "Shela": 1.05
    },

    "Gandhinagar": {
        "GIFT City": 1.40,
        "Kudasan": 1.18,
        "Sargasan": 1.15,
        "Raysan": 1.12,
        "Randesan": 1.08,
        "Koba": 1.00,
        "Adalaj": 0.95,
        "Vavol": 0.92,
        "PDPU Road": 1.10
    },

    "Rajkot": {
        "Raiya": 1.20,
        "Yogi Nagar": 1.18,
        "Munjka": 1.12,
        "Mavdi": 1.05,
        "Madhapar": 1.08,
        "Sadar": 1.10,
        "Bhakti Nagar": 0.95,
        "Mota Mava": 1.12,
        "Vavdi": 0.92,
        "Ghanteshwer": 0.90
    }
}

# ==========================================
# DATA GENERATION
# ==========================================

data = []

for i in range(500):

    city = random.choice(list(locations.keys()))
    area_name = random.choice(list(locations[city].keys()))

    area_factor = locations[city][area_name]

    area = random.randint(700, 3000)

    bhk = random.choices(
        [1, 2, 3, 4, 5],
        weights=[5, 25, 40, 25, 5]
    )[0]

    if bhk == 1:
        bathrooms = random.choice([1, 1, 2])
    elif bhk == 2:
        bathrooms = random.choice([2, 2, 2, 3])
    elif bhk == 3:
        bathrooms = random.choice([2, 3, 3])
    elif bhk == 4:
        bathrooms = random.choice([3, 4])
    else:
        bathrooms = random.choice([4, 5])

    gym = random.choice([0, 1])
    swimming_pool = random.choice([0, 1])
    cinema_hall = random.choice([0, 1])
    gaming_zone = random.choice([0, 1])

    # ======================================
    # PRICE CALCULATION
    # ======================================

    base_price = (
        15
        + area * 0.025
        + bhk * 5
        + bathrooms * 2
    )

    amenity_bonus = (
        gym * 3
        + swimming_pool * 5
        + cinema_hall * 4
        + gaming_zone * 3
    )

    price = (
        base_price * area_factor
        + amenity_bonus
        + random.uniform(-5, 5)
    )

    price = round(max(price, 20), 2)

    data.append([
        i + 1,
        city,
        area_name,
        area,
        bhk,
        bathrooms,
        gym,
        swimming_pool,
        cinema_hall,
        gaming_zone,
        price
    ])

# ==========================================
# CREATE DATAFRAME
# ==========================================

columns = [
    "id",
    "City",
    "Area_Name",
    "Area",
    "BHK",
    "Bathrooms",
    "Gym",
    "Swimming_Pool",
    "Cinema_Hall",
    "Gaming_Zone",
    "Price"
]

df = pd.DataFrame(data, columns=columns)

# ==========================================
# SAVE DATASET
# ==========================================

os.makedirs("dataset", exist_ok=True)

df.to_csv(
    "dataset/house_data.csv",
    index=False
)

print("======================================")
print("500 ROW DATASET CREATED SUCCESSFULLY")
print("======================================")

print("\nDataset Shape:")
print(df.shape)

print("\nFirst 5 Rows:")
print(df.head())

print("\nCities:")
print(df["City"].value_counts())

print("\nDataset saved at:")
print("dataset/house_data.csv")




