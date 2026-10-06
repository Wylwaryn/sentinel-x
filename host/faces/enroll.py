"""Sentinel-X — enrôlement des visages de l'équipe (personnes autorisées).

Usage (depuis host/faces, PC hôte, webcam USB libre : arrêter la vision avant) :
  python enroll.py              enrôler une personne : connexion ADMIN, choix du compte, photos à la webcam
  python enroll.py sync         reconstruire la galerie locale depuis les images ACTIVES du dashboard
  python enroll.py list         voir qui est dans la galerie locale

Les comptes se créent dans la VM : sudo ../dashboard/api/add-user.sh <email> "<nom>" <ROLE>
Consentement : n'enrôler que des personnes d'accord (donnée biométrique, RGPD art. 9).
"""
import argparse
import getpass
import json
import sys
from pathlib import Path

import cv2

from dashboard_client import DashboardClient, DashboardError
from faces import FaceEngine, face_crop
from gallery import Gallery
from sync import sync_once

BASE = Path(__file__).resolve().parent
GALLERY = BASE / "data" / "gallery.npz"
VISION = BASE.parent / "vision"
PHOTOS = 5
MIN_FACE_PX = 90       # visage trop petit = empreinte peu fiable
MIN_SCORE = 0.90


def open_camera():
    sys.path.insert(0, str(VISION))
    from camera import open_camera as vision_open  # mêmes réglages (index, exposition) que la vision
    cfg = json.loads((VISION / "config.json").read_text(encoding="utf-8"))["camera"]
    return vision_open(cfg)


def connect():
    client = DashboardClient()
    email = input("Email ADMIN du dashboard : ").strip()
    password = getpass.getpass("Mot de passe : ")
    try:
        user = client.login(email, password)
    finally:
        del password
    print(f"Connecté : {user['nom']} ({user['role']})")
    return client


def choose_user(client):
    users = [u for u in client.users() if u["actif"]]
    for k, u in enumerate(users, 1):
        print(f"  {k}. {u['nom']} <{u['email']}> ({u['role']})")
    while True:
        choice = input(f"Personne à enrôler (1-{len(users)}) : ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(users):
            return users[int(choice) - 1]


def cmd_enroll(_args):
    client = connect()
    person = choose_user(client)
    engine, gallery = FaceEngine(), Gallery(GALLERY)
    cap = open_camera()
    done, message = 0, "Regardez la camera, ESPACE pour photographier"
    hints = ["de face", "legerement a gauche", "legerement a droite", "menton leve", "de face, plus pres"]
    try:
        while done < PHOTOS:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.resize(frame, (640, 480))
            faces = engine.detect(frame)
            usable = len(faces) == 1 and faces[0][2] >= MIN_FACE_PX and faces[0][14] >= MIN_SCORE
            view = frame.copy()
            for f in faces:
                x, y, w, h = (int(v) for v in f[:4])
                cv2.rectangle(view, (x, y), (x + w, y + h), (0, 200, 0) if usable else (0, 0, 255), 2)
            status = ("OK" if usable else "1 seul visage, plus pres" if faces else "aucun visage")
            cv2.putText(view, f"{person['nom']} : photo {done + 1}/{PHOTOS} ({hints[done]})", (10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.putText(view, f"{status} | {message}", (10, 470), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            cv2.imshow("Sentinel-X - enrolement (ESPACE photo, ECHAP quitter)", view)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                break
            if key == 32:
                if not usable:
                    message = "photo refusee : un seul visage net et assez grand"
                    continue
                jpeg = face_crop(frame, faces[0])
                vec = engine.embed(frame, faces[0])
                try:
                    id_image = client.upload(person["id_utilisateur"], jpeg)
                except DashboardError as exc:
                    message = f"envoi refuse : {exc}"
                    continue
                gallery.add(person["id_utilisateur"], person["nom"], person["role"], vec, image_id=id_image)
                done += 1
                message = f"photo enregistree (image {id_image})"
    finally:
        cap.release()
        cv2.destroyAllWindows()
        client.logout()
    print(f"{done} photo(s) enregistrée(s) pour {person['nom']}. Galerie : "
          f"{len(gallery.people())} personne(s), {len(gallery)} empreinte(s).")


def cmd_sync(_args):
    """Reconstruit la galerie depuis les images ACTIVES du dashboard (désactivation = retrait)."""
    client = connect()
    engine, gallery = FaceEngine(), Gallery(GALLERY)
    gallery.remove_images([-1], save=False)  # empreintes d'origine inconnue : remplacées par celles du dashboard
    added, removed, skipped = sync_once(gallery, engine, client)
    gallery.save()
    client.logout()
    print(f"Galerie synchronisée : +{added}, -{removed} ; {len(gallery.people())} personne(s), "
          f"{len(gallery)} empreinte(s)" + (f", {skipped} image(s) sans visage ignorée(s)" if skipped else ""))


def cmd_list(_args):
    gallery = Gallery(GALLERY)
    if not len(gallery):
        print("Galerie vide.")
    for pid, (name, role, n) in gallery.people().items():
        print(f"  {pid:>3}  {name} ({role}) : {n} empreinte(s)")


def main():
    parser = argparse.ArgumentParser(description="Enrôlement des visages Sentinel-X")
    parser.add_argument("cmd", nargs="?", default="enroll", choices=["enroll", "sync", "list"])
    args = parser.parse_args()
    try:
        {"enroll": cmd_enroll, "sync": cmd_sync, "list": cmd_list}[args.cmd](args)
    except DashboardError as exc:
        print(f"Dashboard : {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
