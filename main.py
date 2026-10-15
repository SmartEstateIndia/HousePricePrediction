import pandas as pd
import matplotlib.pyplot as plt
import joblib
import os

from sklearn.model_selection import train_test_split
from sklearn.linear_model import LinearRegression
from sklearn.tree import DecisionTreeRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


# ==========================================
# 1. LOAD DATASET
# ==========================================

print("==========================================")
print("       HOUSE PRICE PREDICTION")
print("==========================================")

df = pd.read_csv("dataset/house_data.csv")

print("\nFirst 5 rows:")
print(df.head())

print("\nDataset Information:")
print(df.info())

print("\nMissing Values:")
print(df.isnull().sum())

print("\nDataset Shape:")
print(df.shape)


# ==========================================
# 2. REMOVE DUPLICATES
# ==========================================

print("\nDuplicate Rows:", df.duplicated().sum())

df = df.drop_duplicates()


# ==========================================
# 3. BASIC EDA
# ==========================================

# Area vs Price
plt.figure(figsize=(8, 5))
plt.scatter(df["Area"], df["Price"])
plt.xlabel("Area (sq.ft.)")
plt.ylabel("Price (Lakh)")
plt.title("Area vs House Price")
plt.grid(True)
plt.show()


# BHK vs Price
plt.figure(figsize=(8, 5))
plt.scatter(df["BHK"], df["Price"])
plt.xlabel("BHK")
plt.ylabel("Price (Lakh)")
plt.title("BHK vs House Price")
plt.grid(True)
plt.show()


# ==========================================
# 4. PREPROCESSING
# ==========================================

# Convert City and Area into numerical columns
df = pd.get_dummies(
    df,
    columns=["City", "Area_Name"],
    dtype=int
)


# ==========================================
# 5. FEATURES AND TARGET
# ==========================================

X = df.drop("Price", axis=1)
y = df["Price"]


# ==========================================
# 6. TRAIN TEST SPLIT
# ==========================================

X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.20,
    random_state=42
)


print("\nTraining Data:", X_train.shape)
print("Testing Data:", X_test.shape)


# ==========================================
# 7. LINEAR REGRESSION
# ==========================================

linear_model = LinearRegression()

linear_model.fit(X_train, y_train)

linear_prediction = linear_model.predict(X_test)

linear_mae = mean_absolute_error(
    y_test,
    linear_prediction
)

linear_mse = mean_squared_error(
    y_test,
    linear_prediction
)

linear_rmse = linear_mse ** 0.5

linear_r2 = r2_score(
    y_test,
    linear_prediction
)


# ==========================================
# 8. DECISION TREE
# ==========================================

tree_model = DecisionTreeRegressor(
    random_state=42,
    max_depth=6
)

tree_model.fit(X_train, y_train)

tree_prediction = tree_model.predict(X_test)

tree_mae = mean_absolute_error(
    y_test,
    tree_prediction
)

tree_mse = mean_squared_error(
    y_test,
    tree_prediction
)

tree_rmse = tree_mse ** 0.5

tree_r2 = r2_score(
    y_test,
    tree_prediction
)


# ==========================================
# 9. RANDOM FOREST
# ==========================================

forest_model = RandomForestRegressor(
    n_estimators=100,
    random_state=42,
    max_depth=8
)

forest_model.fit(X_train, y_train)

forest_prediction = forest_model.predict(X_test)

forest_mae = mean_absolute_error(
    y_test,
    forest_prediction
)

forest_mse = mean_squared_error(
    y_test,
    forest_prediction
)

forest_rmse = forest_mse ** 0.5

forest_r2 = r2_score(
    y_test,
    forest_prediction
)


# ==========================================
# 10. MODEL COMPARISON
# ==========================================

print("\n==========================================")
print("           MODEL COMPARISON")
print("==========================================")

print("\nLinear Regression")
print("MAE :", round(linear_mae, 2))
print("MSE :", round(linear_mse, 2))
print("RMSE:", round(linear_rmse, 2))
print("R2  :", round(linear_r2, 2))


print("\nDecision Tree")
print("MAE :", round(tree_mae, 2))
print("MSE :", round(tree_mse, 2))
print("RMSE:", round(tree_rmse, 2))
print("R2  :", round(tree_r2, 2))


print("\nRandom Forest")
print("MAE :", round(forest_mae, 2))
print("MSE :", round(forest_mse, 2))
print("RMSE:", round(forest_rmse, 2))
print("R2  :", round(forest_r2, 2))


# ==========================================
# 11. SELECT BEST MODEL
# ==========================================

models = {
    "Linear Regression": (linear_model, linear_r2),
    "Decision Tree": (tree_model, tree_r2),
    "Random Forest": (forest_model, forest_r2)
}

best_model_name = max(
    models,
    key=lambda x: models[x][1]
)

best_model = models[best_model_name][0]

print("\n==========================================")
print("Best Model:", best_model_name)
print("R2 Score:", round(models[best_model_name][1], 2))
print("==========================================")


# ==========================================
# 12. SAVE BEST MODEL
# ==========================================

os.makedirs("model", exist_ok=True)

joblib.dump(
    best_model,
    "model/best_model.pkl"
)

joblib.dump(
    list(X.columns),
    "model/model_columns.pkl"
)

print("\nBest model saved successfully!")
print("Feature columns saved successfully!")


# ==========================================
# 13. HOUSE PRICE PREDICTION
# ==========================================

print("\n==========================================")
print("       ENTER HOUSE DETAILS")
print("==========================================")

area = float(
    input("Enter Area (sq.ft.): ")
)

bhk = int(
    input("Enter BHK: ")
)

bathrooms = int(
    input("Enter Bathrooms: ")
)

city = input(
    "Enter City (Ahmedabad/Gandhinagar/Rajkot): "
)

# Areas according to city

city_areas = {
    "Ahmedabad": [
        "Bodakdev",
        "Thaltej",
        "Satellite",
        "Vastrapur",
        "Prahlad Nagar",
        "Science City",
        "Ambli",
        "Bopal",
        "South Bopal",
        "Gota",
        "Chandkheda",
        "Nikol",
        "Vastral",
        "Maninagar",
        "Motera",
        "Shela"
    ],

    "Gandhinagar": [
        "GIFT City",
        "Kudasan",
        "Sargasan",
        "Raysan",
        "Randesan",
        "Koba",
        "Adalaj",
        "Vavol",
        "PDPU Road"
    ],

    "Rajkot": [
        "Raiya",
        "Yogi Nagar",
        "Munjka",
        "Mavdi",
        "Madhapar",
        "Sadar",
        "Bhakti Nagar",
        "Mota Mava",
        "Vavdi",
        "Ghanteshwer"
    ]
}


if city not in city_areas:
    print("Invalid city entered!")
    exit()


print("\nAvailable Areas:")

for area_name in city_areas[city]:
    print("-", area_name)

selected_area = input(
    "\nEnter Area Name: "
)


if selected_area not in city_areas[city]:
    print("Invalid area entered!")
    exit()


# ==========================================
# AMENITIES
# ==========================================

gym = int(
    input("Gym? (1=Yes, 0=No): ")
)

swimming_pool = int(
    input("Swimming Pool? (1=Yes, 0=No): ")
)

cinema_hall = int(
    input("Cinema Hall? (1=Yes, 0=No): ")
)

gaming_zone = int(
    input("Gaming Zone? (1=Yes, 0=No): ")
)


# ==========================================
# CREATE INPUT DATA
# ==========================================

new_house = pd.DataFrame({
    "id": [0],
    "Area": [area],
    "BHK": [bhk],
    "Bathrooms": [bathrooms],
    "Gym": [gym],
    "Swimming_Pool": [swimming_pool],
    "Cinema_Hall": [cinema_hall],
    "Gaming_Zone": [gaming_zone]
})


# Add City columns
for city_name in [
    "Ahmedabad",
    "Gandhinagar",
    "Rajkot"
]:
    new_house[
        "City_" + city_name
    ] = 0


# Add Area columns
all_areas = []

for areas in city_areas.values():
    all_areas.extend(areas)


for area_name in all_areas:
    new_house[
        "Area_Name_" + area_name
    ] = 0


# Set selected City = 1
new_house[
    "City_" + city
] = 1


# Set selected Area = 1
new_house[
    "Area_Name_" + selected_area
] = 1


# Arrange columns exactly like training data
new_house = new_house[X.columns]


# ==========================================
# PREDICT PRICE
# ==========================================

predicted_price = best_model.predict(
    new_house
)[0]


# ==========================================
# RESULT
# ==========================================

print("\n==========================================")
print("        HOUSE PRICE PREDICTION")
print("==========================================")

print("City :", city)
print("Area :", selected_area)
print("Size :", area, "sq.ft.")
print("BHK  :", bhk)
print("Bathrooms :", bathrooms)

print("------------------------------------------")

print(
    f"Estimated Price: ₹{predicted_price:.2f} Lakh"
)

print("==========================================")