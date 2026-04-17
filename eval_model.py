import os
import cv2
import numpy as np
import tensorflow as tf
import ast

model_path = '/root/Projects/Pikamon/autocatcher/Fuzzybot-v8.1/PY/banerus/pokemonModel_fuzzy.h5'
labels_path = '/root/Projects/Pikamon/autocatcher/Fuzzybot-v8.1/PY/banerus/labels.txt'
cache_dir = '/root/Projects/Pikamon/src/data/spawn_cache'

# We don't have a direct ID->Name map, but we can make a rough guess
# based on standard national dex numbering up to 898 to evaluate accuracy.
# However, we'll just check if the predicted name is somewhat correct.
# A better way: let's fetch pokedex data from the internet to evaluate properly.
import urllib.request
import json
try:
    req = urllib.request.urlopen("https://raw.githubusercontent.com/Purukitto/pokemon-data.json/master/pokedex.json")
    pokedex = json.loads(req.read())
    id_to_name = {str(p["id"]): p["name"]["english"] for p in pokedex}
except:
    id_to_name = {}

with open(labels_path, 'r', encoding='utf-8') as f:
    labels = ast.literal_eval(f.read().strip())

model = tf.keras.models.load_model(model_path, compile=False)
images = [f for f in os.listdir(cache_dir) if f.endswith('.jpg')]

correct = 0
total = 0
unknown = 0

for img in images:
    img_path = os.path.join(cache_dir, img)
    orig = cv2.imread(img_path, cv2.IMREAD_UNCHANGED)
    if orig is None: continue
        
    resized = cv2.resize(orig, (200, 125)) / 255.0
    resized = np.expand_dims(resized, axis=0)
    
    preds = model.predict(resized, verbose=0)
    idx = np.argmax(preds)
    pred_name = labels[idx]
    
    # Check accuracy based on filename
    base_id = img.replace('.jpg', '').replace('shiny_', '')
    true_name = id_to_name.get(base_id)
    
    if true_name:
        total += 1
        # Fuzzy or exact match
        if pred_name.lower() in true_name.lower() or true_name.lower() in pred_name.lower():
            correct += 1
    else:
        unknown += 1

if total > 0:
    print(f"Evaluated {total} recognizable images (excluding {unknown} unknown IDs).")
    print(f"Accuracy: {correct/total:.2%} ({correct}/{total})")
else:
    print("Could not evaluate properly.")
