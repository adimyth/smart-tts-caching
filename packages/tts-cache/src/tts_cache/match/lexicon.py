"""Per-language word lists for the Level B diff classifier (SOLUTION.md 5).

Fillers decide what may be served as a ``filler`` difference, so the lists
are short and conservative: tokens that carry no content when they lead or
trail a sentence. Names, dates and currency words mark a ``slot``, which is
never served; a slot classified as ``other`` is equally never served, so these
lists only sharpen the histogram and cannot make the cache serve more.
"""

import re

FILLERS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "okay ok okey so well alright um umm uh uhh hmm yeah yes oh actually basically sure great anyway"
        " right".split()
    ),
    "hi": frozenset("अच्छा ठीक तो हाँ हां जी हम्म अरे बस ओके चलिए ज़रूर अच्छाजी".split()),
    "ta": frozenset("சரி ஓகே ம்ம் ஆமா ஆமாம் அதாவது சரிங்க அப்படியா சரிசரி".split()),
    "as": frozenset(
        """
    অ' আচ্ছা এহ ও'কে গতিকে চাওক ছাৰ তেন্তে দেখোন নহয় ব'লক বাইদেউ বাৰু মানে মেডাম হ'ব হয় হুম
    """.split()
    ),
    "bn": frozenset(
        """
    আচ্ছা আরে আসলে ওকে চলুন জি ঠিক তাহলে তো দেখুন ব্যস মানে ম্যাডাম স্যার হুম হ্যাঁ
    """.split()
    ),
    "gu": frozenset(
        """
    અચ્છા અરે ઓકે ચાલો જી ઠીક તો બરાબર બસ મેડમ સારું સાહેબ હમ્મ હવે હા હાં
    """.split()
    ),
    "kn": frozenset(
        """
    ಅಂದರೆ ಅಯ್ಯೋ ಅಲ್ವಾ ಆಗಲಿ ಆಯ್ತು ಓಕೆ ನೋಡಿ ಬನ್ನಿ ಮತ್ತೆ ಮೇಡಂ ಸರಿ ಸರ್ ಹಾಂ ಹೌದು ಹ್ಮ್ಮ್
    """.split()
    ),
    "ml": frozenset(
        """
    അതെ അപ്പോൾ അയ്യോ അല്ലേ ആ ഉം എന്നാൽ ഓക്കേ ചേച്ചി ചേട്ടാ പിന്നെ മാഡം ശരി സർ ഹലോ ഹ്മ്
    """.split()
    ),
    "mr": frozenset(
        """
    अच्छा अरे ओके खरंच चला जी तर बघा बरं बस मग मॅडम म्हणजे साहेब हं हम्म हो
    """.split()
    ),
    "or": frozenset(
        """
    ଆଚ୍ଛା ଆଜ୍ଞା ଆରେ ଓକେ ଚାଲନ୍ତୁ ଠିକ୍ ତ ତେବେ ଦେଖନ୍ତୁ ନା ବସ୍ ମାନେ ହଁ ହୁଁ
    """.split()
    ),
    "pa": frozenset(
        """
    ਅਰੇ ਅੱਛਾ ਓਕੇ ਚਲੋ ਜੀ ਤਾਂ ਦੇਖੋ ਬਸ ਮਤਲਬ ਵੈਸੇ ਸੁਣੋ ਹਾਂ ਹਾਂਜੀ ਹੂੰ ਹੈਂ
    """.split()
    ),
    "te": frozenset(
        """
    అండి అంతే అయితే అరె అలాగే అవును ఆ ఊ ఓకే కదా చూడండి పదండి మరి మేడమ్ సరే సరేనా సర్ హ్మ్
    """.split()
    ),
}
# Multi-word fillers that only count as a whole.
FILLER_PHRASES: dict[str, frozenset[str]] = {
    "en": frozenset({"all right", "you know", "i mean", "okay so", "so yeah"}),
    "hi": frozenset({"ठीक है", "अच्छा जी", "हाँ जी", "जी हाँ", "ओके जी"}),
    "ta": frozenset({"சரி சரி", "சரிங்க சார்", "ஓகே சார்"}),
    "as": frozenset({"কি কওঁ", "ঠিক আছে", "ঠিক আছে নে", "ঠিকেই আছে", "তাৰ পাছত", "হয় নে", "হয় হয়"}),
    "bn": frozenset({"ও আচ্ছা", "জি হ্যাঁ", "ঠিক আছে", "তাই তো", "যাই হোক", "হ্যাঁ জি"}),
    "gu": frozenset({"ચાલો ત્યારે", "જી હા", "ઠીક છે", "બરાબર છે", "સારું તો", "હા જી"}),
    "kn": frozenset({"ಅದು ಹೌದು", "ಓಕೆ ಸರ್", "ಸರಿ ಮೇಡಂ", "ಸರಿ ಸರಿ", "ಹಾಂ ಸರಿ", "ಹೌದು ಹೌದು"}),
    "ml": frozenset({"അതെ അതെ", "ആ ശരി", "എന്നാൽ ശരി", "ഓക്കേ സർ", "ശരി ശരി", "ശരി സർ"}),
    "mr": frozenset({"अच्छा ठीक आहे", "ठीक आहे", "बरं का", "बरं मग", "हो का", "हो जी", "हो ना"}),
    "or": frozenset({"ଆଜ୍ଞା ହଁ", "ଠିକ୍ ଅଛି", "ଠିକ୍ ତ", "ତା ହେଲେ", "ବୁଝିଲେ ତ", "ହଁ ଆଜ୍ଞା"}),
    "pa": frozenset({"ਇੱਕ ਮਿੰਟ", "ਚੰਗਾ ਜੀ", "ਜੀ ਹਾਂ", "ਠੀਕ ਹੈ", "ਠੀਕ ਹੈ ਜੀ", "ਹਾਂ ਜੀ"}),
    "te": frozenset({"అలాగే అండి", "అవును అండి", "ఓకే సర్", "సరే అండి", "సరే మేడమ్", "సరే సర్"}),
}

# The most frequent English words plus the vocabulary of support conversations:
# a Latin-script token outside this list, in a one-span difference, is name-like.
COMMON_EN = frozenset(
    """
    a about above after again against all am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has
    have having he her here hers herself him himself his how i if in into is it its itself just me
    more most my myself no nor not now of off on once only or other our ours ourselves out over own
    same she should so some such than that the their theirs them themselves then there these they
    this those through to too under until up very was we were what when where which while who whom
    why will with would you your yours yourself yourselves let lets let's it's i'm you're we're
    they're i'll you'll we'll i've you've we've can't don't won't isn't aren't wasn't didn't doesn't
    haven't hasn't couldn't shouldn't wouldn't cannot that's there's here's what's please thank
    thanks thankyou sorry hello hi hey bye goodbye welcome yes no okay sure right good great fine
    happy glad help check explain tell say ask call calling called number account order orders
    refund payment payments delivery address plan support team today tomorrow yesterday morning
    evening afternoon night minute minutes moment second seconds message phone email card balance
    ticket store hour hours week weeks month months year years service request status issue problem
    details detail time date offer price bill charge renewal subscription device network signal app
    website password login profile record history agent specialist manager reply back soon again
    still also only just really quickly safely easily away one two three four five six seven eight
    nine ten first last next new old other same another way thing things anything something
    everything nothing see look find found got get give given go going come coming make made take
    taken know need want like may might must shall wait waiting hold confirm confirmed update
    updated send sent receive received process processed done ready arrive arrived ship shipped
    cancel cancelled change changed open close closed day days
    """.split()
)

NAMES: dict[str, frozenset[str]] = {
    "en": frozenset(
        """
    alex sam priya rahul neha amit anita ravi kumar ramesh suresh john mary david sarah james emma
    michael olivia daniel sophia peter lisa mark anna paul laura kevin rachel arjun vikram pooja
    kavya rohan simran meera karthik divya deepa lakshmi anil sunil vijay ajay sanjay rajesh mohan
    arun varun ishaan aditya ananya riya
    """.split()
    ),
    "hi": frozenset(
        """
    प्रिया राहुल नेहा अमित अनीता रवि कुमार रमेश सुरेश विजय अजय संजय राजेश मोहन अरुण वरुण अर्जुन
    विक्रम पूजा काव्या रोहन सिमरन मीरा दिव्या दीपा लक्ष्मी अनिल सुनील आदित्य अनन्या रिया सीमा सुनीता
    गीता राधा श्याम कृष्ण गौरव मनीष नितिन पंकज सौरभ अंकित आकाश विशाल शिवम आरती ज्योति कविता ममता
    रेखा सोनिया शर्मा वर्मा गुप्ता सिंह पटेल मेहता जोशी अग्रवाल यादव मिश्रा तिवारी चौधरी
    """.split()
    ),
    "ta": frozenset(
        """
    குமார் ரமேஷ் சுரேஷ் விஜய் அஜய் ராஜேஷ் மோகன் அருண் வருண் கார்த்திக் பிரியா திவ்யா தீபா லட்சுமி
    மீனா கவிதா சரவணன் முருகன் செந்தில் பாலு கணேஷ் வெங்கட் ஆனந்த் பிரகாஷ் சுந்தர் ராஜா ராணி கீதா
    சித்ரா வாணி அனிதா சுதா ஜெயா மாலா ரவி சந்திரன் சேகர் கண்ணன் சிவா விக்னேஷ் ஹரிஷ் அர்ஜுன் நந்தினி
    ஐஸ்வர்யா கௌதம் அஸ்வின் தினேஷ் மகேஷ் ரம்யா ஷாலினி
    """.split()
    ),
    "as": frozenset(
        """
    অংকুৰ অনুৰাধা অমিয় অয়ন অৰ্পিতা আলী আৰ্যন ইমৰান উৎপল ঋতুপৰ্ণা কলিতা কাকতি কৌশিক গগৈ গীতাঞ্জলি
    চৌধুৰী ছফিকুল জুৰি জ্যোতি ডেকা তপন দাস দিগন্ত দীপক নয়নমণি নিবেদিতা নীলাক্ষী নেওগ পংকজ পল্লবী
    প্ৰণব প্ৰিয়ংকা পৰাগ ফুকন বন্দনা বিকাশ বিজয়া বৰগোহাঁই বৰদলৈ বৰা বৰুৱা বৰ্ণালী ভাস্কৰ মহন্ত মানস
    মিতালী মৃদুলা মৌচুমী শইকীয়া শৰ্মা সঞ্জীব হাজৰিকা হিমাংশু হুছেইন হেমন্ত ৰঞ্জন ৰাহুল ৰুমি
    ৰূপজ্যোতি ৰূপম
    """.split()
    ),
    "bn": frozenset(
        """
    অঙ্কিত অনিতা অমিত অরবিন্দ অর্ণব আয়ুষ আরিফ ইমরান ঋতুপর্ণা কবিতা কুমার কৌশিক গীতা ঘোষ চক্রবর্তী
    চ্যাটার্জি জয়ন্ত তনুশ্রী তাপস দত্ত দাস দীনেশ দেবশ্রী নাসরিন নেহা পার্থ পূজা প্রসেনজিৎ প্রিয়া
    ফারহানা বিজয় বিশ্বজিৎ বোস ব্যানার্জি ভট্টাচার্য মহেশ মালা মিতালি মিত্র মুখার্জি মৌসুমী রমেশ
    রায় রাহুল রুমা রোহিত শর্মিষ্ঠা শ্রাবণী সঞ্জয় সরকার সরিতা সিংহ সীমা সুব্রত সুমন সুরেশ সেন
    সৌমিত্র সৌরভ হালদার
    """.split()
    ),
    "gu": frozenset(
        """
    અંકિત અમિત અરવિંદ અલ્પેશ ઉર્વશી કરણ કવિતા કાજલ કિરણ કોમલ ગોહિલ ચિરાગ ચૌધરી જયેશ જિગ્નેશ જોશી
    ઠક્કર ત્રિવેદી દર્શન દિનેશ દીપિકા દેસાઈ નિરવ નિલેશ નીતા નેહા પંડ્યા પટેલ પાર્થ પૂજા પ્રકાશ
    પ્રિયા ભાવના ભાવેશ મનીષા મયંક મહેતા મહેશ મીના મોદી રમેશ રાકેશ રાજેશ રાવલ રાહુલ રિદ્ધિ રેખા રોહિત
    વિપુલ વિશાલ વ્યાસ શાહ સંજય સિદ્ધિ સુરેશ સ્નેહા હરેશ હાર્દિક હેતલ
    """.split()
    ),
    "kn": frozenset(
        """
    ಅಂಕಿತ್ ಅನನ್ಯಾ ಅನಿತಾ ಅಮಿತ್ ಅರವಿಂದ ಆರವ್ ಉಮಾ ಕಬೀರ್ ಕವಿತಾ ಕಿರಣ್ ಕುಮಾರ್ ಗಿರೀಶ್ ಗೀತಾ ಗೌಡ ಚೇತನ್ ದಿನೇಶ್
    ದೀಪಾ ನವೀನ್ ನಾಗರಾಜ್ ನಾಯಕ್ ನೇಹಾ ಪಲ್ಲವಿ ಪಾಟೀಲ್ ಪೂಜಾ ಪ್ರಕಾಶ್ ಪ್ರವೀಣ್ ಪ್ರಿಯಾ ಬಸವರಾಜ್ ಭಟ್ ಭಾರತಿ
    ಮಂಜುನಾಥ್ ಮನೋಜ್ ಮಹೇಶ್ ಮೀನಾ ರಮೇಶ್ ರವಿ ರಾಜೇಶ್ ರಾವ್ ರಾಹುಲ್ ರೇಖಾ ರೋಹಿತ್ ಲಕ್ಷ್ಮಿ ವಿಜಯ್ ವಿನಯ್ ವೆಂಕಟೇಶ್
    ಶಂಕರ್ ಶರ್ಮಾ ಶಿಲ್ಪಾ ಶೆಟ್ಟಿ ಶ್ರೀನಿವಾಸ್ ಸಂಜಯ್ ಸರಸ್ವತಿ ಸರಿತಾ ಸಾನ್ವಿ ಸೀಮಾ ಸುನಿಲ್ ಸುನೀತಾ ಸುರೇಶ್ ಹರೀಶ್
    ಹೆಗ್ಡೆ
    """.split()
    ),
    "ml": frozenset(
        """
    അഖിൽ അജിത്ത് അഞ്ജലി അനൂപ് അബ്ദുൽ അമൽ അരുൻ അശ്വതി ആതിര കിരൻ കുര്യൻ കൃഷ്ണൻ ഗീത ജയേഷ് ജോസഫ് ജോർജ്
    തോമസ് ദിനേശ് ദിവ്യ ദീപക് നായർ നിഖിൽ നീതു പിള്ള പ്രമോദ് പ്രിയ ഫാത്തിമ ബാലകൃഷ്ണൻ ബിജു മനോജ് മഹേഷ്
    മാത്യു മാലതി മുഹമ്മദ് മേനോൻ മോഹൻ രമ രമേശ് രാധാകൃഷ്ണൻ രാമൻ രാഹുൽ രേഖ റഹ്മാൻ ലക്ഷ്മി ലത വിനോദ്
    വിഷ്ണു വർഗീസ് ഷാഫി ഷിബു ഷീല സന്തോഷ് സനൽ സീത സുമതി സുരേഷ് സൈനബ ഹരി
    """.split()
    ),
    "mr": frozenset(
        """
    अंकित अक्षय अजय अनिता अमित अरविंद ऋषिकेश ओंकार कदम कविता काळे कुलकर्णी गायकवाड गीता चव्हाण जगताप
    जाधव जोशी तुषार दिनेश देशपांडे देशमुख निखिल नितीन पवार पाटील पूजा प्रकाश प्रशांत प्रिया भोसले
    मंगेश मधुरा मनीषा महेश मीनाक्षी मोरे रमेश रवी राहुल रूपाली रोहित वाघमारे विजय विनय विशाल वैशाली
    शिंदे शुभम संजय सरिता सविता सागर साळुंखे सावंत सीमा सुनीता सुरेश स्नेहा स्वाती
    """.split()
    ),
    "or": frozenset(
        """
    ଅଙ୍କିତ ଅନିତା ଅମିତ ଅରବିନ୍ଦ ଆଦିତ୍ୟ ଆୟୁଷ କବିତା ଗୀତା ଗୌରାଙ୍ଗ ଚିନ୍ମୟ ଜେନା ଜ୍ୟୋତି ତପନ ଦାସ ଦିନେଶ
    ଦେବାଶିଷ ନାୟକ ନିରଞ୍ଜନ ପଟ୍ଟନାୟକ ପଣ୍ଡା ପରିଡ଼ା ପୂଜା ପ୍ରକାଶ ପ୍ରଧାନ ପ୍ରିୟା ବସନ୍ତ ବାରିକ ବିକାଶ ବିଜୟ
    ବେହେରା ମନୋଜ ମମତା ମହାନ୍ତି ମହାପାତ୍ର ମହେଶ ମାଧବୀ ମିଶ୍ର ରବି ରମେଶ ରଶ୍ମି ରାଉତ ରାଜେଶ ରାହୁଲ ରୋହିତ ଲକ୍ଷ୍ମଣ
    ଲିପିକା ସଞ୍ଜୀବ ସଞ୍ଜୟ ସରିତା ସାମଲ ସାହୁ ସୀମା ସୁଚିତ୍ରା ସୁନୀଲ ସୁବ୍ରତ ସୁରେଶ ସୁଲୋଚନା ସୌମ୍ୟ ସ୍ନେହା ସ୍ୱାଇଁ
    """.split()
    ),
    "pa": frozenset(
        """
    ਅਮਨਦੀਪ ਅਰਸ਼ਦੀਪ ਅਵਤਾਰ ਕਰਨ ਕਰਮਜੀਤ ਕਿਰਨ ਕੁਲਦੀਪ ਕੁਲਵੰਤ ਕੌਰ ਗਰੇਵਾਲ ਗਿੱਲ ਗੁਰਦੀਪ ਗੁਰਪ੍ਰੀਤ ਗੁਰਮੀਤ
    ਗੁਰਿੰਦਰ ਚੀਮਾ ਜਗਜੀਤ ਜਤਿੰਦਰ ਜਸਪ੍ਰੀਤ ਜਸਵੰਤ ਢਿੱਲੋਂ ਤੇਜਿੰਦਰ ਦਵਿੰਦਰ ਨਵਨੀਤ ਨਿਰਮਲ ਨੀਲਮ ਪਰਮਜੀਤ ਪੂਨਮ
    ਪ੍ਰਭਜੋਤ ਬਰਾੜ ਬਲਵਿੰਦਰ ਬਾਜਵਾ ਮਨਜੀਤ ਮਨਪ੍ਰੀਤ ਮਹਿੰਦਰ ਮਾਨ ਰਣਜੀਤ ਰਮਨਦੀਪ ਰਵਿੰਦਰ ਰਾਜਿੰਦਰ ਰੁਪਿੰਦਰ ਰੰਧਾਵਾ
    ਲਵਪ੍ਰੀਤ ਵਿਰਕ ਸਤਨਾਮ ਸਰਬਜੀਤ ਸ਼ਰਮਾ ਸਿਮਰਨ ਸਿੰਘ ਸਿੱਧੂ ਸੁਖਵਿੰਦਰ ਸੁਰਜੀਤ ਸੰਧੂ ਹਰਜਿੰਦਰ ਹਰਜੀਤ ਹਰਪ੍ਰੀਤ
    ਹਰਭਜਨ ਹਰਮਨ
    """.split()
    ),
    "te": frozenset(
        """
    అఖిల అనిత అనిల్ అరుణ్ కవిత కిరణ్ కుమార్ గీత గోపాల్ గౌడ్ చందు చైతన్య చౌదరి తరుణ్ దివ్య నవీన్
    నాయుడు పద్మ పద్మజ ప్రవీణ్ ప్రసాద్ ప్రియ బాబు భవాని భార్గవి భాస్కర్ మహేశ్వరి మహేష్ మురళి మౌనిక
    యాదవ్ రమేష్ రవి రాజు రాజేష్ రాధ రావు రుక్మిణి రెడ్డి రోహిత్ లక్ష్మి లత వరుణ్ వర్మ వెంకట్ శర్మ
    శ్రావణి శ్రీకాంత్ శ్రీనివాస్ శ్రీను సందీప్ సతీష్ సీత సుజాత సుధ సుమ సురేష్ స్వాతి హరిక హరీష్
    """.split()
    ),
}

DATES: dict[str, frozenset[str]] = {
    "en": frozenset(
        """
    monday tuesday wednesday thursday friday saturday sunday january february march april june july
    august september october november december jan feb mar apr jun jul aug sep sept oct nov dec
    today tomorrow yesterday am pm noon midnight morning evening afternoon tonight weekend
    """.split()
    ),
    "hi": frozenset(
        """
    सोमवार मंगलवार बुधवार गुरुवार शुक्रवार शनिवार रविवार जनवरी फ़रवरी फरवरी मार्च अप्रैल मई जून
    जुलाई अगस्त सितंबर अक्टूबर नवंबर दिसंबर आज कल परसों सुबह शाम दोपहर रात बजे तारीख़ तारीख
    """.split()
    ),
    "ta": frozenset(
        """
    திங்கள்கிழமை செவ்வாய்க்கிழமை புதன்கிழமை வியாழக்கிழமை வெள்ளிக்கிழமை சனிக்கிழமை ஞாயிற்றுக்கிழமை
    திங்கள் செவ்வாய் புதன் வியாழன் வெள்ளி சனி ஞாயிறு ஜனவரி பிப்ரவரி மார்ச் ஏப்ரல் மே ஜூன் ஜூலை
    ஆகஸ்ட் செப்டம்பர் அக்டோபர் நவம்பர் டிசம்பர் இன்று நாளை நேற்று காலை மாலை மதியம் இரவு மணி மணிக்கு
    தேதி
    """.split()
    ),
    "as": frozenset(
        """
    অক্টোবৰ আগষ্ট আঘোণ আজি আবেলি আহাৰ আহিন এতিয়া এপ্ৰিল কাইলৈ কাতি গধূলি চ'ত চন চনত ছেপ্টেম্বৰ
    জানুৱাৰী জুন জুলাই জেঠ ডিচেম্বৰ তাৰিখ তাৰিখে দিন দুপৰীয়া দেও দেওবাৰ দেওবাৰে নৱেম্বৰ পুহ পুৱা
    ফাগুন ফেব্ৰুৱাৰী ব'হাগ বছৰ বজাত বজালৈ বজাৰ বাজে বুধ বুধবাৰ বুধবাৰে বৃহস্পতি বৃহস্পতিবাৰ
    বৃহস্পতিবাৰে ভাদ মঙ্গল মঙ্গলবাৰ মঙ্গলবাৰে মাঘ মাজনিশা মাহ মাৰ্চ মে' যোৱাকালি শনি শনিবাৰ শনিবাৰে
    শাওণ শুকুৰ শুকুৰবাৰ শুকুৰবাৰে সন্ধিয়া সপ্তাহ সময় সোম সোমবাৰ সোমবাৰে ৰাতি ৰাতিপুৱা
    """.split()
    ),
    "bn": frozenset(
        """
    অক্টোবর অগ্রহায়ণ আগস্ট আগামীকাল আজ আজকে আশ্বিন আষাঢ় এখন এখনই এপ্রিল কার্তিক কাল গতকাল চৈত্র
    জানুয়ারি জুন জুলাই জ্যৈষ্ঠ টা টায় ডিসেম্বর তারিখ তারিখে দিন দুপুর দুপুরে নভেম্বর পৌষ ফাল্গুন
    ফেব্রুয়ারি বছর বিকেল বিকেলে বুধ বুধবার বৃহস্পতি বৃহস্পতিবার বৈশাখ ভাদ্র ভোর ভোরে মঙ্গল মঙ্গলবার
    মাঘ মার্চ মাস মে রবি রবিবার রাত রাতে শনি শনিবার শুক্র শুক্রবার শ্রাবণ সকাল সকালে সন্ধ্যা
    সন্ধ্যায় সময় সাল সেপ্টেম্বর সোম সোমবার
    """.split()
    ),
    "gu": frozenset(
        """
    અઠવાડિયું અઠવાડિયે અત્યારે અષાઢ આજે આવતીકાલે આસો એપ્રિલ એપ્રિલે ઓક્ટોબર ઓક્ટોબરે ઓગસ્ટ ઓગસ્ટે
    કારતક કાલે ગઈકાલે ગુરુ ગુરુવાર ગુરુવારે ચૈત્ર જાન્યુઆરી જાન્યુઆરીએ જુલાઈ જુલાઈએ જૂન જૂને જેઠ
    ડિસેમ્બર ડિસેમ્બરે તારીખ તારીખે દિવસ દિવસે નવેમ્બર નવેમ્બરે પોષ ફાગણ ફેબ્રુઆરી ફેબ્રુઆરીએ બપોર
    બપોરે બુધ બુધવાર બુધવારે ભાદરવો મંગળ મંગળવાર મંગળવારે મહા મહિને મહિનો માગશર માર્ચ માર્ચે મે મેએ
    રવિ રવિવાર રવિવારે રાત રાત્રે વર્ષ વર્ષે વાગે વાગ્યા વાગ્યે વૈશાખ શનિ શનિવાર શનિવારે શુક્ર
    શુક્રવાર શુક્રવારે શ્રાવણ સપ્ટેમ્બર સપ્ટેમ્બરે સમય સમયે સવાર સવારે સાંજ સાંજે સોમ સોમવાર સોમવારે
    હવે હાલમાં
    """.split()
    ),
    "kn": frozenset(
        """
    ಅಕ್ಟೋಬರ್ ಆಗಸ್ಟ್ ಆದಿತ್ಯವಾರ ಆಶ್ವಯುಜ ಆಷಾಢ ಇಂದು ಇವತ್ತು ಈಗ ಏಪ್ರಿಲ್ ಕಾರ್ತಿಕ ಗಂಟೆ ಗಂಟೆಗೆ ಗಂಟೆಯ ಗುರು
    ಗುರುವಾರ ಚೈತ್ರ ಜನವರಿ ಜುಲೈ ಜೂನ್ ಜ್ಯೇಷ್ಠ ಡಿಸೆಂಬರ್ ತಾರೀಖಿಗೆ ತಾರೀಖು ತಿಂಗಳು ದಿನಾಂಕ ನವೆಂಬರ್ ನಾಡಿದ್ದು
    ನಾಳೆ ನಿನ್ನೆ ಪುಷ್ಯ ಫಾಲ್ಗುಣ ಫೆಬ್ರವರಿ ಬುಧ ಬುಧವಾರ ಬೆಳಗ್ಗೆ ಬೆಳಿಗ್ಗೆ ಭಾದ್ರಪದ ಭಾನು ಭಾನುವಾರ ಮಂಗಳ ಮಂಗಳವಾರ
    ಮಧ್ಯರಾತ್ರಿ ಮಧ್ಯಾಹ್ನ ಮಾಘ ಮಾರ್ಗಶಿರ ಮಾರ್ಚ್ ಮುಂಜಾನೆ ಮೇ ರವಿವಾರ ರಾತ್ರಿ ವರ್ಷ ವಾರ ವೈಶಾಖ ಶನಿ ಶನಿವಾರ ಶುಕ್ರ
    ಶುಕ್ರವಾರ ಶ್ರಾವಣ ಸಂಜೆ ಸಮಯ ಸಾಯಂಕಾಲ ಸೆಪ್ಟೆಂಬರ್ ಸೋಮ ಸೋಮವಾರ
    """.split()
    ),
    "ml": frozenset(
        """
    ആഴ്ച ഇടവം ഇന്നലെ ഇന്ന് ഇപ്പോൾ ഉച്ച ഉച്ചയ്ക്ക് ഏപ്രിൽ ഒക്ടോബർ ഓഗസ്റ്റ് കന്നി കുംഭം കർക്കടകം
    ചിങ്ങം ചൊവ്വ ചൊവ്വാഴ്ച ജനുവരി ജൂലൈ ജൂൺ ഞായറാഴ്ച ഞായർ ഡിസംബർ തിങ്കളാഴ്ച തിങ്കൾ തീയതി തുലാം ദിവസം
    ധനു നവംബർ നാളെ ഫെബ്രുവരി ബുധനാഴ്ച ബുധൻ മകരം മണി മണിക്ക് മറ്റന്നാൾ മാസം മാർച്ച് മിഥുനം
    മിനിഞ്ഞാന്ന് മീനം മെയ് മേടം രാത്രി രാവിലെ വൃശ്ചികം വെള്ളി വെള്ളിയാഴ്ച വൈകിട്ട് വൈകുന്നേരം വ്യാഴം
    വ്യാഴാഴ്ച ശനി ശനിയാഴ്ച സമയം സെപ്റ്റംബർ
    """.split()
    ),
    "mr": frozenset(
        """
    आज आठवडा आता आत्ता आश्विन आषाढ उद्या एप्रिल ऑक्टोबर ऑगस्ट कार्तिक काल गुरु गुरुवार गुरुवारी
    चैत्र जानेवारी जुलै जून ज्येष्ठ डिसेंबर तारखेला तारीख दिनांक दिवशी दिवस दुपार दुपारी नोव्हेंबर
    परवा पौष फाल्गुन फेब्रुवारी बुध बुधवार बुधवारी भाद्रपद मंगळ मंगळवार मंगळवारी महिना माघ
    मार्गशीर्ष मार्च मे रवि रविवार रविवारी रात्र रात्री रोजी वाजता वाजेपर्यंत वेळ वेळी वैशाख शनि
    शनिवार शनिवारी शुक्र शुक्रवार शुक्रवारी श्रावण संध्याकाळ संध्याकाळी सकाळ सकाळी सध्या सप्टेंबर
    सायंकाळी सोम सोमवार सोमवारी
    """.split()
    ),
    "or": frozenset(
        """
    ଅକ୍ଟୋବର ଅଗଷ୍ଟ ଅପରାହ୍ନ ଆଜି ଆଶ୍ୱିନ ଆଷାଢ଼ ଏପ୍ରିଲ ଏବେ କାର୍ତ୍ତିକ କାଲି ଗତକାଲି ଗୁରୁ ଗୁରୁବାର ଚୈତ୍ର
    ଜାନୁଆରୀ ଜୁନ ଜୁଲାଇ ଜ୍ୟେଷ୍ଠ ଟା ଟାରେ ଡିସେମ୍ବର ତାରିଖ ତାରିଖରେ ଦିନ ଦ୍ୱିପ୍ରହର ଦ୍ୱିପ୍ରହରରେ ନଭେମ୍ବର ପରଦିନ
    ପୌଷ ଫାଲ୍‌ଗୁନ ଫେବୃଆରୀ ବର୍ତ୍ତମାନ ବୁଧ ବୁଧବାର ବୈଶାଖ ଭାଦ୍ରବ ମଇ ମଙ୍ଗଳ ମଙ୍ଗଳବାର ମାଘ ମାର୍ଗଶିର ମାର୍ଚ୍ଚ
    ମାସ ରବି ରବିବାର ରାତି ରାତିରେ ଶନି ଶନିବାର ଶୁକ୍ର ଶୁକ୍ରବାର ଶ୍ରାବଣ ସକାଳ ସକାଳେ ସନ୍ଧ୍ୟା ସନ୍ଧ୍ୟାରେ ସପ୍ତାହ
    ସମୟ ସମୟରେ ସେପ୍ଟେମ୍ବର ସୋମ ସୋମବାର
    """.split()
    ),
    "pa": frozenset(
        """
    ਅਕਤੂਬਰ ਅਗਸਤ ਅਪ੍ਰੈਲ ਅੱਜ ਅੱਸੂ ਐਤਵਾਰ ਕੱਤਕ ਕੱਲ ਕੱਲ੍ਹ ਚੇਤ ਜਨਵਰੀ ਜੁਲਾਈ ਜੂਨ ਜੇਠ ਟਾਈਮ ਤਰੀਕ ਤਾਰੀਖ ਦਸੰਬਰ
    ਦੁਪਹਿਰ ਦੁਪਹਿਰੇ ਨਵੰਬਰ ਪਰਸੋਂ ਪੋਹ ਫਰਵਰੀ ਫੱਗਣ ਬੁੱਧ ਬੁੱਧਵਾਰ ਭਾਦੋਂ ਮਈ ਮਹੀਨਾ ਮਾਘ ਮਾਰਚ ਮੰਗਲ ਮੰਗਲਵਾਰ ਮੱਘਰ
    ਰਾਤ ਰਾਤੀਂ ਵਜੇ ਵਿਸਾਖ ਵੀਰ ਵੀਰਵਾਰ ਸਤੰਬਰ ਸਮਾਂ ਸਵੇਰ ਸਵੇਰੇ ਸ਼ਨਿੱਚਰਵਾਰ ਸ਼ਨੀ ਸ਼ਨੀਵਾਰ ਸ਼ਾਮ ਸ਼ਾਮੀਂ ਸ਼ੁੱਕਰ
    ਸ਼ੁੱਕਰਵਾਰ ਸਾਉਣ ਸਾਲ ਸੋਮ ਸੋਮਵਾਰ ਹਫ਼ਤਾ ਹਫ਼ਤੇ ਹਾੜ੍ਹ ਹੁਣ ਹੁਣੇ
    """.split()
    ),
    "te": frozenset(
        """
    అక్టోబర్ ఆగస్టు ఆది ఆదివారం ఆశ్వయుజం ఆషాఢం ఇప్పుడు ఈరోజు ఉదయం ఎల్లుండి ఏప్రిల్ కార్తీకం గంట గంటల
    గంటలకు గంటలు గురు గురువారం చైత్రం జనవరి జూన్ జూలై జ్యేష్ఠం డిసెంబర్ తేదీ తేదీన నవంబర్ నిన్న నెల
    పుష్యం ఫాల్గుణం ఫిబ్రవరి బుధ బుధవారం భాద్రపదం మంగళ మంగళవారం మధ్యాహ్నం మాఘం మార్గశిరం మార్చి మే
    రాత్రి రేపు రోజు వారం వైశాఖం శని శనివారం శుక్ర శుక్రవారం శ్రావణం సమయం సాయంత్రం సెప్టెంబర్ సోమ
    సోమవారం
    """.split()
    ),
}

CURRENCY: frozenset[str] = frozenset(
    """
    $ Rs dollar dollars euro euros inr paise pound pounds rs rupee rupees £ कोटी पैसा पैसे रक्कम रु
    रु. रुपए रुपया रुपयांचा रुपयांची रुपयांचे रुपये लाख हजार কোটি টকা টকাত টকাৰ টাকা টাকায় টাকার
    পইচা পইচাৰ পয়সা রুপি লক্ষ লাখ হাজার হাজাৰ ৰুপী ਕਰੋੜ ਪੈਸਾ ਪੈਸੇ ਰਕਮ ਰੁ. ਰੁਪਈਆ ਰੁਪਈਏ ਰੁਪਏ ਰੁਪਿਆ
    ਲੱਖ ਹਜ਼ਾਰ કરોડ પૈસા પૈસો રકમ રકમનું રૂ રૂ. રૂપિયા રૂપિયાની રૂપિયાનું રૂપિયાનો રૂપિયો લાખ હજાર
    କୋଟି ଟଙ୍କା ଟଙ୍କାର ଟଙ୍କାରେ ଦାମ ପଇସା ପଇସାର ମୂଲ୍ୟ ରାଶି ରୁପି ରୁପିଆ ଲକ୍ଷ ହଜାର பைசா ரூபாய் కోటి కోట్లు
    పైసలు పైసా రూ రూ. రూపాయల రూపాయలతో రూపాయలు రూపాయి లక్ష లక్షలు ಕೋಟಿ ದುಡ್ಡು ಪೈಸೆ ಪೈಸೆಗಳು ಮೊತ್ತ ರೂ
    ರೂ. ರೂಪಾಯಿ ರೂಪಾಯಿಗಳ ರೂಪಾಯಿಗಳು ಲಕ್ಷ ಸಾವಿರ ಹಣ കോടി തുക പൈസ പൈസയാണ് രൂ രൂപ രൂപയാണ് രൂപയും രൂപയുടെ
    രൂപയോളം രൂപയ്ക്ക് ലക്ഷം € ₹
    """.split()
)


# Spelled-out small numbers, which change a fact as surely as a digit does.
NUMBER_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset("two three four five six seven eight nine ten twenty fifty hundred thousand".split()),
    "hi": frozenset("दो तीन चार पाँच पांच छह छः सात आठ नौ दस बीस पचास सौ हज़ार हजार".split()),
    "mr": frozenset("दोन तीन चार पाच सहा सात आठ नऊ दहा वीस पन्नास शंभर हजार".split()),
    "bn": frozenset("দুই তিন চার পাঁচ ছয় সাত আট নয় দশ বিশ পঞ্চাশ শত হাজার".split()),
    "as": frozenset("দুই তিনি চাৰি পাঁচ ছয় সাত আঠ ন দহ বিছ পঞ্চাশ শ হাজাৰ".split()),
    "pa": frozenset("ਦੋ ਤਿੰਨ ਚਾਰ ਪੰਜ ਛੇ ਸੱਤ ਅੱਠ ਨੌਂ ਦਸ ਵੀਹ ਪੰਜਾਹ ਸੌ ਹਜ਼ਾਰ".split()),
    "gu": frozenset("બે ત્રણ ચાર પાંચ છ સાત આઠ નવ દસ વીસ પચાસ સો હજાર".split()),
    "or": frozenset("ଦୁଇ ତିନି ଚାରି ପାଞ୍ଚ ଛଅ ସାତ ଆଠ ନଅ ଦଶ କୋଡ଼ିଏ ପଚାଶ ଶହ ହଜାର".split()),
    "ta": frozenset("இரண்டு மூன்று நான்கு ஐந்து ஆறு ஏழு எட்டு ஒன்பது பத்து இருபது ஐம்பது நூறு ஆயிரம்".split()),
    "te": frozenset("రెండు మూడు నాలుగు ఐదు ఆరు ఏడు ఎనిమిది తొమ్మిది పది ఇరవై యాభై వంద వెయ్యి".split()),
    "kn": frozenset("ಎರಡು ಮೂರು ನಾಲ್ಕು ಐದು ಆರು ಏಳು ಎಂಟು ಒಂಬತ್ತು ಹತ್ತು ಇಪ್ಪತ್ತು ಐವತ್ತು ನೂರು ಸಾವಿರ".split()),
    "ml": frozenset("രണ്ട് മൂന്ന് നാല് അഞ്ച് ആറ് ഏഴ് എട്ട് ഒമ്പത് പത്ത് ഇരുപത് അമ്പത് നൂറ് ആയിരം".split()),
}


# Negation: a sentence that negates and one that does not never mean the same thing,
# however close an encoder scores them. Words are matched whole; suffixes cover the
# Dravidian negative verb endings ("செலுத்தப்படவில்லை", "చేయలేదు").
NEGATIONS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "not no never none nobody nothing neither nor cannot can't don't doesn't didn't won't wouldn't"
        " isn't aren't wasn't weren't hasn't haven't hadn't shouldn't couldn't unable".split()
    ),
    "hi": frozenset("नहीं न ना मत नहि".split()),
    "mr": frozenset("नाही नका नको नव्हता नव्हते नव्हती नसेल न".split()),
    "bn": frozenset("না নয় নেই নাই নি নন নই নও".split()),
    "as": frozenset("নহয় নাই নালাগে নকৰিব নোৱাৰে".split()),
    "pa": frozenset("ਨਹੀਂ ਨਾ ਨਾਂ ਨੀ ਨਹੀ".split()),
    "gu": frozenset("નથી નહીં નહિ ના ન".split()),
    "or": frozenset("ନାହିଁ ନୁହେଁ ନା ନ ନାହିଁ।".split()),
    "ta": frozenset("இல்லை இல்ல வேண்டாம் கிடையாது கூடாது முடியாது".split()),
    "te": frozenset("లేదు కాదు లేవు వద్దు రాదు కాదని లేరు లేను లేము".split()),
    "kn": frozenset("ಇಲ್ಲ ಅಲ್ಲ ಬೇಡ ಆಗಿಲ್ಲ ಇಲ್ಲವೇ ಇಲ್ಲವಾ".split()),
    "ml": frozenset("ഇല്ല അല്ല വേണ്ട ഇല്ലെന്ന്".split()),
}
NEGATION_SUFFIXES: dict[str, tuple[str, ...]] = {
    "ta": (
        "வில்லை",
        "இல்லை",
        "மாட்டேன்",
        "மாட்டோம்",
        "மாட்டார்",
        "கூடாது",
        "முடியாது",
        "ாதீங்க",
        "ாதீர்கள்",
        "ாதீர்",
        "ாதே",
        "ில்லை",
        "ாது",
    ),
    "te": ("లేదు", "లేరు", "లేవు", "కాదు", "కపోవచ్చు", "లేను", "లేము", "కూడదు", "వద్దు"),
    "kn": ("ಇಲ್ಲ", "ಅಲ್ಲ", "ಬಾರದು", "ಿಲ್ಲ", "ಿಲ್ಲವೇ", "ಿಲ್ಲವಾ", "ಬೇಡ", "ಬೇಡಿ"),
    "ml": ("ഇല്ല", "ില്ല", "അല്ല", "രുത്"),
}
# Negative endings a fixed suffix cannot state. Kannada ಅಲ್ಲ fuses with a word ending in
# the inherent vowel ("ನಿಮ್ಮದಲ್ಲ", "ಬರಲ್ಲ"), leaving consonant + ಲ್ಲ; the consonant must
# carry no vowel sign, and something must precede it, so "ಎಲ್ಲ", "ಅವರೆಲ್ಲ" and "ಬೆಲ್ಲ" stay affirmative.
# The Telugu negative imperative is a stem in the inherent vowel plus కండి ("చేయకండి",
# "నొక్కకండి", "తీసుకోకండి"); the affirmative of a stem ending in క is క + ండి, whose క
# follows a virama or a short vowel sign ("నొక్కండి", "పీకండి"), so కండి must follow a plain
# consonant or a long ా or ో.
NEGATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "kn": re.compile(".+[ಕ-ಹ]ಲ್ಲ(?:ವ[ೇಾ])?"),
    "te": re.compile(".+[క-హాో]కండి"),
}
# Affirmatives a suffix or pattern would otherwise take: the Kannada ability auxiliary, an
# infinitive in the inherent vowel plus ಬಲ್ಲ ("ಮಾಡಬಲ್ಲ", "can do"); and a Tamil loanword in
# -ill with the accusative ஐ, one syllable after an optional cluster ("பில்லை" "the bill",
# "ஸ்கில்லை"), where a fused இல்லை always follows a longer word ("தேவையில்லை", "பணமில்லை").
NEGATION_EXCEPTIONS: dict[str, re.Pattern[str]] = {
    "kn": re.compile(".*[ಕ-ಹ]ಬಲ್ಲ(?:ವ[ೇಾ])?"),
    "ta": re.compile("(?:[க-ஹ]்)?[க-ஹ]ில்லை"),
}


def is_negation(token: str, lang: str) -> bool:
    if token in NEGATIONS.get(lang, frozenset()):
        return True
    exception = NEGATION_EXCEPTIONS.get(lang)
    if exception is not None and exception.fullmatch(token) is not None:
        return False
    if any(token.endswith(suf) and len(token) > len(suf) for suf in NEGATION_SUFFIXES.get(lang, ())):
        return True
    pattern = NEGATION_PATTERNS.get(lang)
    return pattern is not None and pattern.fullmatch(token) is not None


def has_word(token: str, words: frozenset[str], *, max_suffix: int = 8) -> bool:
    """True when ``token`` is a listed word, or a listed word of three or more characters plus a case suffix.

    Indic languages attach case and postposition suffixes to nouns and names
    ("मार्चमध्ये", "ಮಾರ್ಚ್‌ನಲ್ಲಿ", "রাহুলবাবু"), so the lists are matched as
    prefixes too.
    """
    if token in words:
        return True
    return any(len(w) >= 3 and token.startswith(w) and len(token) - len(w) <= max_suffix for w in words)


def lang_of(language: str) -> str:
    return language.replace("_", "-").split("-")[0].lower()
