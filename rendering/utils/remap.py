UR5e_demo = []
Jaco_demo = []
IIWA_demo = []
Sawyer_demo = []
Kinova3_demo = []
demokey_pairing = [0, 1, 10, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 11, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 12, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 13, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 14, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 15, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 16, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 17, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179, 18, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 19, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 2, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 3, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 4, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 5, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 6, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 7, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 8, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 9, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99]

Jaco = [116, 188]
IIWA = [    50,
    53,
    55,
    80,
    88,
    101,
    116,
    120,
    126,
    160,
    172,
    186,
    188,
    191]
UR5e = [116]
Sawyer = [116]
Kinova3 = [20, 82, 116, 186, 187]

for j in Jaco:
    Jaco_demo.append(demokey_pairing[j])

for i in IIWA:
    IIWA_demo.append(demokey_pairing[i])

for u in UR5e:
    UR5e_demo.append(demokey_pairing[u])

for s in Sawyer:
    Sawyer_demo.append(demokey_pairing[s])

for k in Kinova3:
    Kinova3_demo.append(demokey_pairing[k])

Jaco_demo.sort()
IIWA_demo.sort()
UR5e_demo.sort()
Sawyer_demo.sort()
Kinova3_demo.sort()

print('Jaco:', Jaco_demo)
print('IIWA:', IIWA_demo)
print('UR5e:', UR5e_demo)
print('Sawyer:', Sawyer_demo)
print('Kinova3:', Kinova3_demo)

